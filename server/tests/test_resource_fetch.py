"""受限资源脚本和出站契约测试；网络替身仅用于确定性边界，不冒称真实下载。"""
import asyncio
import gzip
import hashlib
import io
import json
import socket
import threading
import time
import zlib

import httpx
import pytest
from fastapi import HTTPException
from homeai import resource_fetch as resources


@pytest.mark.parametrize('url', [
    'file:///etc/passwd', 'http://example.com/file', 'https://user:pass@example.com/file',
    'https://localhost/file', 'https://host.docker.internal/file', 'https://home.local/file',
    'https://example.com/a%0d%0aHost:other', 'https://example.com/x?api_key=private-value',
    'https://example.com/x?email=someone%40example.com',
])
@pytest.mark.asyncio
async def test_non_public_or_sensitive_urls_fail_before_network(monkeypatch, url):
    async def no_dns(*args):
        pytest.fail('无效地址不应进入DNS或网络层')
    monkeypatch.setattr(resources, '_resolve_public', no_dns)
    with pytest.raises(HTTPException):
        await resources.fetch_public(url, max_bytes=100)


@pytest.mark.parametrize('ip', ['127.0.0.1', '10.0.0.1', '172.16.0.1', '192.168.1.1', '169.254.169.254',
                               '100.64.0.1', '0.0.0.0', '224.0.0.1', '::1', 'fc00::1', 'fe80::1',
                               '::ffff:127.0.0.1', '64:ff9b::7f00:1', '2002:7f00:1::'])
def test_private_reserved_and_translation_addresses_are_not_public(ip):
    assert not resources._public_ip(ip)


@pytest.mark.asyncio
async def test_mixed_dns_answers_reject_entire_connection(monkeypatch):
    async def lookup(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', lookup)
    with pytest.raises(resources.ResourceFetchError) as failure:
        await resources._resolve_public('example.com', 443)
    assert failure.value.code == 'private_address'


def test_tls_connects_pinned_ip_but_verifies_original_hostname(monkeypatch):
    seen = {}
    class RawSocket:
        def settimeout(self, value): seen['timeout'] = value
        def connect(self, target): seen['address'] = target
        def close(self): seen['closed'] = True
    raw = RawSocket()
    monkeypatch.setattr(resources.socket, 'socket', lambda *args: raw)
    class Context:
        check_hostname = True
        def wrap_socket(self, sock, *, server_hostname):
            assert sock is raw
            seen['server_hostname'] = server_hostname
            return raw
    monkeypatch.setattr(resources.ssl, 'create_default_context', Context)
    connection = resources._PinnedHTTPSConnection('example.com', 443,
        resolved=(socket.AF_INET, ('93.184.216.34', 443)), timeout=2)
    connection.connect()
    assert seen['address'] == ('93.184.216.34', 443)
    assert seen['server_hostname'] == 'example.com'
    connection.close()


@pytest.mark.asyncio
async def test_redirects_are_checked_and_provenance_never_keeps_signed_query(monkeypatch):
    hosts, requests = [], []
    async def lookup(host, port):
        hosts.append(host)
        if host == 'internal.example.com':
            raise resources.ResourceFetchError('private_address', '解析到内网', 403)
        return socket.AF_INET, ('93.184.216.34', port)
    def read(url, resolved, maximum, deadline, canceled):
        requests.append(url)
        if len(requests) == 1:
            return {'redirect': 'https://cdn.example.com/file%20name.txt?Signature=PUBLIC-OPAQUE&Expires=123'}
        return {'raw': b'public file', 'mime_type': 'text/plain'}
    monkeypatch.setattr(resources, '_resolve_public', lookup)
    monkeypatch.setattr(resources, '_read_response', read)
    raw, metadata = await resources.fetch_public('https://example.com/start?Signature=FIRST', max_bytes=100)
    assert raw == b'public file' and hosts == ['example.com', 'cdn.example.com']
    assert metadata['name'] == 'file name.txt'
    assert metadata['sha256'] == hashlib.sha256(raw).hexdigest()
    assert 'Signature' not in json.dumps(metadata) and 'PUBLIC-OPAQUE' not in json.dumps(metadata)
    assert metadata['source_url'] == 'https://example.com/start'
    assert metadata['final_url'] == 'https://cdn.example.com/file%20name.txt'
    requests.clear()
    monkeypatch.setattr(resources, '_read_response', lambda *args: requests.append(args[0]) or {'redirect': 'https://internal.example.com/metadata'})
    with pytest.raises(resources.ResourceFetchError) as blocked:
        await resources.fetch_public('https://example.com/start', max_bytes=100)
    assert blocked.value.code == 'private_address' and requests == ['https://example.com/start']


@pytest.mark.asyncio
async def test_untrusted_redirect_scheme_and_redirect_loops_do_not_fetch(monkeypatch):
    calls = []
    async def lookup(host, port): return socket.AF_INET, ('93.184.216.34', port)
    monkeypatch.setattr(resources, '_resolve_public', lookup)
    for target, expected in [('file:///etc/passwd', 'invalid_url'), ('https://example.com/start', 'redirect_loop')]:
        calls.clear()
        monkeypatch.setattr(resources, '_read_response', lambda *args: calls.append(args[0]) or {'redirect': target})
        with pytest.raises(resources.ResourceFetchError) as failure:
            await resources.fetch_public('https://example.com/start', max_bytes=100)
        assert failure.value.code == expected and len(calls) == 1


def test_stream_read_enforces_limit_without_credentials_or_proxy(monkeypatch):
    seen = []
    class Response:
        status = 200
        def __init__(self): self.data = io.BytesIO(b'0123456789')
        def getheader(self, key, default=None): return {'Content-Type': 'text/plain'}.get(key, default)
        def read(self, length): return self.data.read(length)
    class Connection:
        sock = None
        def __init__(self, *args, **kwargs): pass
        def request(self, method, target, headers): seen.append((method, target, headers))
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(resources, '_PinnedHTTPSConnection', Connection)
    with pytest.raises(resources.ResourceFetchError) as failure:
        resources._read_response('https://example.com/file', (socket.AF_INET, ('93.184.216.34', 443)), 5,
                                 time.monotonic() + 5, threading.Event())
    assert failure.value.code == 'size_limit'
    assert not {'Authorization', 'Cookie', 'Proxy-Authorization', 'Referer'} & seen[0][2].keys()
    assert seen[0][2]['Accept-Encoding'] == 'identity'


def install_fetch(monkeypatch, files):
    calls = []
    async def fetch(url, *, max_bytes, allowed_urls=None):
        calls.append(url)
        raw, mime = files[url]
        if len(raw) > max_bytes:
            raise resources.ResourceFetchError('size_limit', '资源超限', 413)
        return raw, {'source_url': resources.public_url(url), 'final_url': resources.public_url(url),
                     'name': 'public-resource', 'mime_type': mime, 'size': len(raw),
                     'sha256': hashlib.sha256(raw).hexdigest(), 'content_trust': 'untrusted_public_resource'}
    monkeypatch.setattr(resources, 'fetch_public', fetch)
    return calls


@pytest.mark.asyncio
async def test_resource_script_downloads_selected_derived_images_and_records_origin(monkeypatch):
    page = 'https://example.com/gallery'
    image = 'https://cdn.example.com/public.png?Signature=VALUE'
    calls = install_fetch(monkeypatch, {page: (b'<script src="https://evil.invalid/run.js"></script><img src="https://cdn.example.com/public.png?Signature=VALUE"><img src="file:///private/data">', 'text/html'), image: (b'public-image-fixture', 'image/png')})
    script = 'page = fetch_resource("https://example.com/gallery")\nlinks = extract_links(page, kind="image")\nresult = select_resources(links, limit=1)'
    result = await resources.execute_resource_script(script, allowed_urls=[page])
    assert calls == [page, image] and len(result) == 1
    assert result[0][0] == b'public-image-fixture'
    assert result[0][1]['discovered_from'] == page and result[0][1]['resource_kind'] == 'image'
    assert 'Signature' not in json.dumps(result[0][1])


@pytest.mark.asyncio
async def test_file_links_skip_navigation_and_video_links_are_bounded(monkeypatch):
    page = 'https://example.com/resources'
    document = 'https://example.com/manual.pdf'
    video = 'https://example.com/movie.mp4'
    calls = install_fetch(monkeypatch, {page: (b'<a href="/about">navigation</a><a href="/manual.pdf">PDF</a><video><source src="/movie.mp4"></video>', 'text/html'),
                                       document: (b'%PDF-public', 'application/pdf'), video: (b'public-video', 'video/mp4')})
    for kind, expected in [('file', document), ('video', video)]:
        calls.clear()
        script = f'page = fetch_resource("{page}")\nresult = select_resources(extract_links(page, kind="{kind}"), limit=1)'
        result = await resources.execute_resource_script(script, allowed_urls=[page])
        assert calls == [page, expected] and result[0][1]['resource_kind'] == kind


@pytest.mark.parametrize('script', [
    'import os\nresult = fetch_resource("https://example.com/file")',
    'result = open("/etc/passwd")', 'result = eval("1+1")', 'result = exec("pass")',
    'result = fetch_resource.__globals__', 'result = [x for x in []]',
    'while True:\n  result = 1', 'result = __import__("os")',
    'result = fetch_resource("https://example.com/file")[0]',
    'fetch_resource = "replacement"',
    'result = select_resources(fetch_resource("https://example.com/file"), limit=1, limit=2)',
])
@pytest.mark.asyncio
async def test_unsupported_scripts_are_rejected_before_any_fetch(monkeypatch, script):
    async def forbidden(*args, **kwargs): pytest.fail('非法脚本不能开始网络访问')
    monkeypatch.setattr(resources, 'fetch_public', forbidden)
    with pytest.raises(resources.ResourceFetchError):
        await resources.execute_resource_script(script, allowed_urls=['https://example.com/file'])


@pytest.mark.asyncio
async def test_script_cannot_invent_urls_or_exceed_total_budget(monkeypatch):
    calls = install_fetch(monkeypatch, {'https://example.com/a.txt': (b'a' * 6, 'text/plain'), 'https://example.com/b.txt': (b'b' * 6, 'text/plain')})
    with pytest.raises(resources.ResourceFetchError) as invented:
        await resources.execute_resource_script('result = fetch_resource("https://other.example.com/file")', allowed_urls=['https://example.com/a.txt'])
    assert invented.value.code == 'url_not_authorized' and calls == []
    with pytest.raises(resources.ResourceFetchError) as budget:
        await resources.execute_resource_script('result = [fetch_resource("https://example.com/a.txt"), fetch_resource("https://example.com/b.txt")]',
            allowed_urls=['https://example.com/a.txt', 'https://example.com/b.txt'], max_bytes=10, total_bytes=10)
    assert budget.value.code == 'size_limit' and len(calls) == 2


@pytest.mark.asyncio
async def test_duplicate_resources_fetch_once_and_no_result_is_not_success(monkeypatch):
    url = 'https://example.com/a.txt'
    calls = install_fetch(monkeypatch, {url: (b'public', 'text/plain')})
    result = await resources.execute_resource_script('a = fetch_resource("https://example.com/a.txt")\nresult = [a, a]', allowed_urls=[url, 'http://unsupported.example.com/'])
    assert len(calls) == 1 and len(result) == 1
    with pytest.raises(resources.ResourceFetchError) as missing:
        await resources.execute_resource_script('links = "nothing"', allowed_urls=[url])
    assert missing.value.code == 'missing_result'


@pytest.mark.asyncio
async def test_only_entire_fake_ip_dns_set_uses_public_doh(monkeypatch):
    calls = []
    async def lookup(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.18.0.205', 443)),
                (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('2001:2::c9', 443, 0, 0))]
    async def doh(host, port):
        calls.append((host, port))
        return socket.AF_INET, ('93.184.216.34', port)
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', lookup)
    monkeypatch.setattr(resources, '_public_doh', doh)
    resolved = await resources._resolve_public('www.example.com', 443)
    assert calls == [('www.example.com', 443)] and resolved[1][0] == '93.184.216.34'


@pytest.mark.asyncio
@pytest.mark.parametrize('second', ['127.0.0.1', '192.168.1.1', '93.184.216.34'])
async def test_fake_ip_mixed_with_other_ranges_never_falls_back(monkeypatch, second):
    async def lookup(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.18.0.1', 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', (second, 443))]
    async def forbidden(*args): pytest.fail('混合DNS地址不能触发公共DNS绕过')
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', lookup)
    monkeypatch.setattr(resources, '_public_doh', forbidden)
    with pytest.raises(resources.ResourceFetchError) as failure:
        await resources._resolve_public('www.example.com', 443)
    assert failure.value.code == 'private_address'


@pytest.mark.asyncio
async def test_literal_fake_ip_is_not_reinterpreted_as_public_hostname(monkeypatch):
    async def forbidden(*args): pytest.fail('显式保留地址不能触发DoH')
    monkeypatch.setattr(resources, '_public_doh', forbidden)
    with pytest.raises(resources.ResourceFetchError) as failure:
        await resources._resolve_public('198.18.0.1', 443)
    assert failure.value.code == 'private_address'


def install_doh(monkeypatch, variation=None):
    calls, configurations = [], []
    real_client = httpx.AsyncClient
    def response(request):
        calls.append(request)
        assert request.url.scheme == 'https' and request.url.host == '1.1.1.1'
        assert request.headers['accept'] == 'application/dns-json'
        assert not any(name in request.headers for name in ('authorization', 'cookie', 'proxy-authorization'))
        kind = 1 if request.url.params['type'] == 'A' else 28
        value = {'Status': 0, 'Question': [{'name': 'www.example.com.', 'type': kind}], 'Answer': []}
        if kind == 1:
            value['Answer'] = [{'name': 'www.example.com.', 'type': 5, 'data': 'cdn.example.com.'},
                               {'name': 'cdn.example.com.', 'type': 1, 'data': '93.184.216.34'}]
        if variation == 'private_ipv4' and kind == 1: value['Answer'][1]['data'] = '10.0.0.1'
        if variation == 'private_ipv6' and kind == 28: value['Answer'] = [{'name': 'www.example.com.', 'type': 28, 'data': '::1'}]
        if variation == 'mismatched_question': value['Question'][0]['name'] = 'unrelated.example.com.'
        if variation == 'unrelated_answer' and kind == 1: value['Answer'][1]['name'] = 'unrelated.example.com.'
        if variation == 'truncated': value['TC'] = True
        if variation == 'invalid_status': value['Status'] = False
        if variation == 'redirect': return httpx.Response(302, headers={'Location': 'https://other.example.com/'})
        if variation == 'oversize': value['Comment'] = 'x' * 70000
        return httpx.Response(200, json=value)
    def client(**kwargs):
        configurations.append(kwargs)
        return real_client(**kwargs, transport=httpx.MockTransport(response))
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    return calls, configurations


@pytest.mark.asyncio
async def test_doh_validates_question_and_cname_chain_without_credentials(monkeypatch):
    calls, configurations = install_doh(monkeypatch)
    resolved = await resources._public_doh('www.example.com', 443)
    assert resolved == (socket.AF_INET, ('93.184.216.34', 443))
    assert len(calls) == 2
    assert configurations[0]['trust_env'] is False and configurations[0]['follow_redirects'] is False
    assert configurations[0].get('verify', True) is not False


@pytest.mark.asyncio
@pytest.mark.parametrize('variation', ['private_ipv4', 'private_ipv6', 'mismatched_question', 'unrelated_answer',
                                       'truncated', 'invalid_status', 'redirect', 'oversize'])
async def test_doh_failure_or_non_public_answer_remains_closed(monkeypatch, variation):
    install_doh(monkeypatch, variation)
    with pytest.raises(resources.ResourceFetchError):
        await resources._public_doh('www.example.com', 443)


def compressed_response(monkeypatch, wire, encoding, *, chunk=65536, declared=True, on_read=None):
    reads = []
    class Response:
        status = 200
        def __init__(self): self.data = io.BytesIO(wire)
        def getheader(self, key, default=None):
            headers = {'Content-Encoding': encoding, 'Content-Type': 'text/html'}
            if declared is not False:
                headers['Content-Length'] = str(len(wire) if declared is True else declared)
            return headers.get(key, default)
        def read(self, length):
            reads.append(length)
            if on_read:
                on_read()
            return self.data.read(min(length, chunk))
    class Connection:
        sock = None
        def __init__(self, *args, **kwargs): pass
        def request(self, *args, **kwargs): pass
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(resources, '_PinnedHTTPSConnection', Connection)
    return reads


@pytest.mark.parametrize('encoding,compress', [('gzip', gzip.compress), ('deflate', zlib.compress)])
@pytest.mark.parametrize('chunk', [1, 7, 65536])
def test_gzip_and_deflate_are_incrementally_decoded_with_exact_output_limit(monkeypatch, encoding, compress, chunk):
    raw = b'<html><img src="/public.png"></html>' * 4
    wire = compress(raw)
    compressed_response(monkeypatch, wire, encoding, chunk=chunk)
    result = resources._read_response('https://example.com/', (socket.AF_INET, ('93.184.216.34', 443)),
                                     len(raw), time.monotonic() + 5, threading.Event())
    assert result['raw'] == raw
    assert result['wire_size'] == len(wire) and result['content_encoding'] == encoding


@pytest.mark.parametrize('encoding,compress', [('gzip', gzip.compress), ('deflate', zlib.compress)])
def test_compressed_bomb_hits_decoded_limit_before_whole_payload_materializes(monkeypatch, encoding, compress):
    wire = compress(b'A' * (4 * 1024 * 1024))
    compressed_response(monkeypatch, wire, encoding, declared=False)
    with pytest.raises(resources.ResourceFetchError) as failure:
        resources._read_response('https://example.com/', (socket.AF_INET, ('93.184.216.34', 443)),
                                 1024, time.monotonic() + 5, threading.Event())
    assert failure.value.code == 'size_limit'


def test_compressed_wire_budget_is_bounded_even_before_first_output(monkeypatch):
    # 合法gzip扩展头可消耗压缩输入而没有输出，仍必须受传输字节上限约束。
    header = b'\x1f\x8b\x08\x04' + b'\x00' * 4 + b'\x00\xff' + (65535).to_bytes(2, 'little') + b'x' * 65535
    compressed_response(monkeypatch, header, 'gzip', declared=False)
    with pytest.raises(resources.ResourceFetchError) as failure:
        resources._read_response('https://example.com/', (socket.AF_INET, ('93.184.216.34', 443)),
                                 2, time.monotonic() + 5, threading.Event())
    assert failure.value.code == 'size_limit'


@pytest.mark.parametrize('encoding,compress', [('gzip', gzip.compress), ('deflate', zlib.compress)])
@pytest.mark.parametrize('problem', ['truncated', 'trailing', 'concatenated', 'checksum', 'declared_mismatch'])
def test_incomplete_corrupt_or_trailing_compressed_data_is_rejected(monkeypatch, encoding, compress, problem):
    wire = compress(b'public text')
    declared = True
    if problem == 'truncated': wire = wire[:-1]
    if problem == 'trailing': wire += b'extra'
    if problem == 'concatenated': wire += compress(b'more')
    if problem == 'checksum': wire = wire[:-1] + bytes([wire[-1] ^ 1])
    if problem == 'declared_mismatch': declared = len(wire) + 1
    compressed_response(monkeypatch, wire, encoding, chunk=3, declared=declared)
    with pytest.raises(resources.ResourceFetchError) as failure:
        resources._read_response('https://example.com/', (socket.AF_INET, ('93.184.216.34', 443)),
                                 1024, time.monotonic() + 5, threading.Event())
    assert failure.value.code in {'incomplete_compressed_resource', 'compressed_trailing_data', 'invalid_compressed_resource', 'incomplete_resource'}


@pytest.mark.parametrize('encoding', ['br', 'gzip, deflate', 'unknown'])
def test_unsupported_compression_is_not_silently_decoded(monkeypatch, encoding):
    reads = compressed_response(monkeypatch, b'not decoded', encoding)
    with pytest.raises(resources.ResourceFetchError) as failure:
        resources._read_response('https://example.com/', (socket.AF_INET, ('93.184.216.34', 443)),
                                 1024, time.monotonic() + 5, threading.Event())
    assert failure.value.code == 'unsupported_encoding' and reads == []


@pytest.mark.parametrize('cause', ['cancel', 'deadline'])
def test_compression_respects_cancellation_and_deadline(monkeypatch, cause):
    canceled = threading.Event()
    clock = [100.0]
    monkeypatch.setattr(resources.time, 'monotonic', lambda: clock[0])
    def stop():
        if cause == 'cancel': canceled.set()
        else: clock[0] = 106.0
    compressed_response(monkeypatch, gzip.compress(b'public text'), 'gzip', on_read=stop)
    with pytest.raises(resources.ResourceFetchError) as failure:
        resources._read_response('https://example.com/', (socket.AF_INET, ('93.184.216.34', 443)), 1024, 105.0, canceled)
    assert failure.value.code == 'download_timeout'
