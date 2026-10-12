"""公网资源下载与受限资源脚本；不是任意 Python、Shell 或浏览器执行器。"""
import ast
import asyncio
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

from fastapi import HTTPException
from .privacy import PII, ensure_model_safe

MAX_RESOURCE_BYTES = 200 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024
FAKE_IP_NETWORKS = (ipaddress.ip_network('198.18.0.0/15'), ipaddress.ip_network('2001:2::/48'))
DOH_ENDPOINT = 'https://1.1.1.1/dns-query'
REDIRECTS = {301, 302, 303, 307, 308}
FILE_SUFFIXES = ('.pdf', '.txt', '.md', '.csv', '.json', '.xml', '.zip', '.tar', '.gz', '.epub',
                 '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.rtf', '.odt', '.ods',
                 '.mp3', '.wav', '.ogg', '.ttf', '.otf', '.woff', '.woff2')


class ResourceFetchError(HTTPException):
    def __init__(self, code, detail, status_code=422):
        super().__init__(status_code, detail)
        self.code = code


def _url(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 8192:
        raise ResourceFetchError('invalid_url', '资源地址为空或过长')
    decoded = unquote(value)
    if any(ord(char) < 33 or ord(char) == 127 for char in value) or any(ord(char) < 32 or ord(char) == 127 for char in decoded):
        raise ResourceFetchError('invalid_url', '资源地址不能包含空白或控制字符')
    ensure_model_safe(decoded)
    if PII.search(decoded):
        raise ResourceFetchError('private_url', '资源地址含个人信息，不能作为公开资源下载', 403)
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if parsed.scheme.lower() != 'https' or not host or parsed.username is not None or parsed.password is not None:
            raise ValueError()
        if '%' in host or host.endswith('.') or (port is not None and not 1 <= port <= 65535):
            raise ValueError()
        host = host.encode('idna').decode('ascii').lower()
    except (ValueError, UnicodeError):
        raise ResourceFetchError('invalid_url', '仅接受不含登录凭据的有效 HTTPS 资源地址') from None
    if host in {'localhost', 'host.docker.internal', 'gateway.docker.internal'} or host.endswith(('.localhost', '.local', '.internal', '.lan', '.home')):
        raise ResourceFetchError('private_address', '不能下载本机或家庭内网资源', 403)
    netloc = ('[' + host + ']' if ':' in host else host) + (':' + str(port) if port and port != 443 else '')
    path = quote(parsed.path or '/', safe="/:@!$&'()*+,;=-._~%")
    query = quote(parsed.query, safe="/:?@!$&'()*+,;=-._~%")
    return urlunsplit(('https', netloc, path, query, ''))


def public_url(value):
    """来源展示只保留域名与路径；所有查询参数（包括签名）均不持久化。"""
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))


def _authorized_urls(values):
    if not isinstance(values, (list, tuple, set)) or not 1 <= len(values) <= 100:
        raise ResourceFetchError('no_public_sources', '请先提供明确公开地址或取得真实联网搜索来源')
    result = set()
    for value in values:
        try:
            result.add(_url(value))
        except HTTPException:
            # 搜索中混入不支持的地址不授权该地址，也不抹掉其他真实可用来源。
            continue
    if not result:
        raise ResourceFetchError('no_public_sources', '本次来源中没有可下载的公开 HTTPS 地址')
    return result


def _public_ip(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if not address.is_global or address.is_multicast or address.is_unspecified or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped or address.sixtofour or address.teredo:
            return False
        if any(address in ipaddress.ip_network(prefix) for prefix in ('64:ff9b::/96', '64:ff9b:1::/48')):
            return False
    return True


def _fake_ip(value):
    try:
        address = ipaddress.ip_address(value)
        return any(address in network for network in FAKE_IP_NETWORKS)
    except ValueError:
        return False


async def _public_doh(host, port):
    """只向固定公网IP查询A/AAAA；TLS验证、无代理、无重定向，不转发资源正文。"""
    import httpx
    async def query(client, kind):
        try:
            async with client.stream('GET', DOH_ENDPOINT, params={'name': host, 'type': kind},
                                     headers={'Accept': 'application/dns-json', 'Accept-Encoding': 'identity'}) as response:
                if response.status_code != 200 or response.headers.get('content-encoding', 'identity').lower() != 'identity':
                    raise ValueError()
                payload = bytearray()
                async for block in response.aiter_bytes():
                    payload.extend(block)
                    if len(payload) > 65536:
                        raise ValueError()
            value = json.loads(payload)
            expected = 1 if kind == 'A' else 28
            if not isinstance(value, dict) or type(value.get('Status')) is not int or value['Status'] != 0 or value.get('TC') is True:
                raise ValueError()
            questions = value.get('Question')
            if not isinstance(questions, list) or len(questions) != 1 or questions[0].get('name', '').rstrip('.').lower() != host.lower() or type(questions[0].get('type')) is not int or questions[0]['type'] != expected:
                raise ValueError()
            answer = value.get('Answer', [])
            if not isinstance(answer, list) or len(answer) > 64 or any(not isinstance(item, dict) for item in answer):
                raise ValueError()
            # 只使用本次问题及其CNAME链上的地址，不能被无关附加记录替换目标。
            names = {host.lower()}
            for _ in range(8):
                linked = {row['data'].rstrip('.').lower() for row in answer
                          if row.get('type') == 5 and isinstance(row.get('name'), str)
                          and row['name'].rstrip('.').lower() in names and isinstance(row.get('data'), str)}
                if linked <= names:
                    break
                names.update(linked)
            addresses = []
            for row in answer:
                if type(row.get('type')) is not int:
                    raise ValueError()
                if row.get('type') not in {1, 28}:
                    continue
                if row['type'] != expected or not isinstance(row.get('name'), str) or row['name'].rstrip('.').lower() not in names or not isinstance(row.get('data'), str):
                    raise ValueError()
                addresses.append(row['data'])
            return addresses
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError, UnicodeError):
            raise ResourceFetchError('dns_unavailable', '公网DNS复核不可用，未连接虚拟或内网地址', 504) from None
    async with httpx.AsyncClient(http2=True, timeout=8, follow_redirects=False, trust_env=False,
                                limits=httpx.Limits(max_connections=2)) as client:
        try:
            groups = await asyncio.wait_for(asyncio.gather(query(client, 'A'), query(client, 'AAAA')), timeout=10)
        except TimeoutError:
            raise ResourceFetchError('dns_unavailable', '公网DNS复核超时，未连接虚拟或内网地址', 504) from None
    values = [value for group in groups for value in group]
    if not values or any(not _public_ip(value) for value in values):
        raise ResourceFetchError('private_address', '公网DNS未返回完全可用的公网地址，已拒绝连接', 403)
    # 优先IPv4，避免只因家庭网络没有原生IPv6导致资源不可达。
    address = sorted((ipaddress.ip_address(value) for value in values), key=lambda value: value.version)[0]
    return (socket.AF_INET, (str(address), port)) if address.version == 4 else (socket.AF_INET6, (str(address), port, 0, 0))


async def _resolve_public(host, port):
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _public_ip(host):
            raise ResourceFetchError('private_address', '不能连接内网、回环或保留地址', 403)
        return (socket.AF_INET, (host, port)) if literal.version == 4 else (socket.AF_INET6, (host, port, 0, 0))
    try:
        values = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM), timeout=5)
    except (OSError, TimeoutError):
        raise ResourceFetchError('dns_unavailable', '无法及时解析公开资源地址', 504) from None
    if values and all(_fake_ip(item[4][0]) for item in values):
        return await _public_doh(host, port)
    if not values or any(not _public_ip(item[4][0]) for item in values):
        raise ResourceFetchError('private_address', '资源地址解析到内网、回环或保留地址，已拒绝连接', 403)
    family, _, _, _, endpoint = values[0]
    return family, endpoint


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, *, resolved, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self.resolved = resolved

    def connect(self):
        # 连接已校验的数字地址，不再次解析域名；TLS仍按原域名校验证书。
        family, endpoint = self.resolved
        raw = socket.socket(family, socket.SOCK_STREAM)
        try:
            raw.settimeout(self.timeout)
            raw.connect(endpoint)
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _read_response(url, resolved, maximum, deadline, canceled):
    parsed = urlsplit(url)
    connection = _PinnedHTTPSConnection(parsed.hostname, parsed.port or 443, resolved=resolved,
                                        timeout=max(0.1, min(10, deadline - time.monotonic())))
    try:
        if canceled.is_set() or time.monotonic() >= deadline:
            raise ResourceFetchError('download_timeout', '资源下载已取消或超时', 504)
        target = parsed.path + ('?' + parsed.query if parsed.query else '')
        connection.request('GET', target, headers={'User-Agent': 'HomeAI-Resource/1.0', 'Accept': '*/*',
                                                   'Accept-Encoding': 'identity', 'Connection': 'close'})
        response = connection.getresponse()
        if response.status in REDIRECTS:
            location = response.getheader('Location')
            if not location:
                raise ResourceFetchError('invalid_redirect', '资源重定向缺少有效地址', 502)
            return {'redirect': location}
        if response.status != 200:
            raise ResourceFetchError('http_error', '资源服务返回 HTTP ' + str(response.status), 502)
        encoding = response.getheader('Content-Encoding', 'identity').lower().strip() or 'identity'
        if encoding not in {'identity', 'gzip', 'deflate'}:
            raise ResourceFetchError('unsupported_encoding', '资源压缩编码暂不支持；仅支持 gzip 和标准 deflate，br 不会被自动放行', 502)
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == 'gzip' else zlib.MAX_WBITS) if encoding != 'identity' else None
        # 压缩传输允许少量封装开销，但压缩前后均有独立硬上限，不依赖压缩比猜测。
        wire_limit = min(MAX_RESOURCE_BYTES, maximum + 64 * 1024) if decoder else maximum
        declared = response.getheader('Content-Length')
        if declared is not None:
            if len(declared) > 20 or not declared.isdecimal():
                raise ResourceFetchError('invalid_size', '资源长度响应无效', 502)
            if int(declared) > wire_limit:
                raise ResourceFetchError('size_limit', '资源传输量超过本次下载大小限制', 413)
        raw, wire_size = bytearray(), 0
        def active():
            if canceled.is_set() or time.monotonic() >= deadline:
                raise ResourceFetchError('download_timeout', '资源下载或解压已取消或超时', 504)
        def append(value):
            raw.extend(value)
            if len(raw) > maximum:
                raise ResourceFetchError('size_limit', '资源解压后超过本次下载大小限制', 413)
        while True:
            remaining = deadline - time.monotonic()
            active()
            if connection.sock:
                connection.sock.settimeout(max(0.1, min(5, remaining)))
            block = response.read(min(64 * 1024, wire_limit + 1 - wire_size))
            if not block:
                break
            wire_size += len(block)
            if wire_size > wire_limit:
                raise ResourceFetchError('size_limit', '资源传输量超过本次下载大小限制', 413)
            if decoder is None:
                append(block)
                continue
            if decoder.eof:
                raise ResourceFetchError('compressed_trailing_data', '压缩资源末尾存在额外数据或多个压缩成员', 502)
            pending = block
            while pending:
                active()
                try:
                    # 每次最多生成64KiB，不能先整段解压再检查大小。
                    value = decoder.decompress(pending, min(64 * 1024, maximum + 1 - len(raw)))
                except zlib.error:
                    raise ResourceFetchError('invalid_compressed_resource', '压缩资源校验失败或编码无效', 502) from None
                append(value)
                if decoder.unused_data:
                    raise ResourceFetchError('compressed_trailing_data', '压缩资源末尾存在额外数据或多个压缩成员', 502)
                pending = decoder.unconsumed_tail
        active()
        if decoder and not decoder.eof:
            raise ResourceFetchError('incomplete_compressed_resource', '压缩资源被截断或缺少完整性校验尾部', 502)
        if not raw or (declared is not None and wire_size != int(declared)):
            raise ResourceFetchError('incomplete_resource', '资源为空或未完整下载', 502)
        mime = response.getheader('Content-Type', 'application/octet-stream').split(';', 1)[0].strip().lower()
        if not re.fullmatch(r'[a-z0-9.+-]+/[a-z0-9.+-]+', mime):
            mime = 'application/octet-stream'
        return {'raw': bytes(raw), 'mime_type': mime, 'wire_size': wire_size, 'content_encoding': encoding}
    except ResourceFetchError:
        raise
    except (TimeoutError, socket.timeout):
        raise ResourceFetchError('download_timeout', '公开资源下载超时', 504) from None
    except (OSError, http.client.HTTPException):
        raise ResourceFetchError('network_error', '公开资源连接或安全证书检查失败', 502) from None
    finally:
        connection.close()


async def fetch_public(url, *, max_bytes, allowed_urls=None):
    """仅下载公网HTTPS，逐跳检查DNS并钉定IP，不携带Cookie、代理或Core凭据。"""
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_RESOURCE_BYTES:
        raise ResourceFetchError('invalid_limit', '单个资源限制必须介于1字节与200MiB之间')
    current = _url(url)
    if allowed_urls is not None and current not in _authorized_urls(allowed_urls):
        raise ResourceFetchError('url_not_authorized', '资源地址不在本次公开来源范围内', 403)
    initial, chain, seen = current, [], set()
    deadline = time.monotonic() + 45
    canceled = threading.Event()
    try:
        for _ in range(4):
            if current in seen:
                raise ResourceFetchError('redirect_loop', '资源重定向形成循环', 502)
            seen.add(current)
            parsed = urlsplit(current)
            resolved = await _resolve_public(parsed.hostname, parsed.port or 443)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ResourceFetchError('download_timeout', '公开资源下载超时', 504)
            response = await asyncio.wait_for(asyncio.to_thread(_read_response, current, resolved, max_bytes, deadline, canceled), timeout=remaining)
            if 'redirect' in response:
                chain.append(public_url(current))
                current = _url(urljoin(current, response['redirect']))
                continue
            raw = response['raw']
            # 元信息由下载器建立，不能信任文件名头部或页面提供的散列。
            name = unquote(urlsplit(current).path.rsplit('/', 1)[-1]) or 'resource'
            name = re.sub(r'[\\/\x00-\x1f\x7f]', '_', name)[:180]
            if name in {'.', '..'}:
                name = 'resource'
            return raw, {'source_url': public_url(initial), 'final_url': public_url(current),
                         'redirects': chain, 'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw),
                         'mime_type': response['mime_type'], 'name': name,
                         'wire_size': response.get('wire_size', len(raw)), 'content_encoding': response.get('content_encoding', 'identity'),
                         'retrieved_at': datetime.now(timezone.utc).isoformat(), 'content_trust': 'untrusted_public_resource'}
        raise ResourceFetchError('redirect_limit', '资源重定向次数超过三次', 502)
    except TimeoutError:
        raise ResourceFetchError('download_timeout', '公开资源下载超时', 504) from None
    finally:
        canceled.set()


@dataclass(frozen=True)
class _Resource:
    raw: bytes
    metadata: dict


@dataclass(frozen=True)
class _Link:
    url: str
    source: str
    kind: str


@dataclass(frozen=True)
class _Links:
    values: tuple[_Link, ...]


class _LinkParser(HTMLParser):
    def __init__(self, base, kind):
        super().__init__(convert_charrefs=True)
        self.base, self.kind, self.links, self.seen = base, kind, [], set()
        self.video_depth = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'video':
            self.video_depth += 1
        raw = None
        if self.kind == 'image' and tag == 'img':
            raw = attributes.get('src')
        elif self.kind == 'video' and (tag == 'video' or (tag == 'source' and self.video_depth)):
            raw = attributes.get('src')
        elif tag == 'a':
            candidate = attributes.get('href') or ''
            try:
                suffix = urlsplit(candidate).path.lower()
            except ValueError:
                return
            if (self.kind == 'file' and ('download' in attributes or suffix.endswith(FILE_SUFFIXES))) or (self.kind == 'image' and suffix.endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif'))) or (self.kind == 'video' and suffix.endswith(('.mp4', '.mov', '.webm'))):
                raw = candidate
        if not raw or raw.startswith('#') or len(self.links) >= 200:
            return
        try:
            url = _url(urljoin(self.base, raw))
        except (HTTPException, ValueError):
            return
        if url not in self.seen:
            self.seen.add(url)
            self.links.append(_Link(url, self.base, self.kind))

    def handle_endtag(self, tag):
        if tag == 'video':
            self.video_depth = max(0, self.video_depth - 1)


async def execute_resource_script(source, *, allowed_urls, max_resources=3, max_bytes=20 * 1024 * 1024, total_bytes=50 * 1024 * 1024):
    """解释赋值及三个固定资源函数，不执行Python字节码，不接收私人资料或宿主文件。"""
    if not isinstance(source, str) or not 1 <= len(source.encode()) <= 8192:
        raise ResourceFetchError('script_limit', '受限资源脚本必须介于1字节与8KiB之间')
    if type(max_resources) is not int or not 1 <= max_resources <= 5 or type(total_bytes) is not int or not 1 <= total_bytes <= MAX_TOTAL_BYTES or type(max_bytes) is not int or not 1 <= max_bytes <= MAX_RESOURCE_BYTES:
        raise ResourceFetchError('invalid_limit', '资源数量或总大小限制无效')
    authorized = _authorized_urls(allowed_urls)
    try:
        tree = ast.parse(source, mode='exec')
    except (SyntaxError, ValueError, RecursionError):
        raise ResourceFetchError('invalid_script', '受限资源脚本语法无效') from None
    permitted = (ast.Module, ast.Assign, ast.Expr, ast.Name, ast.Constant, ast.Call, ast.keyword, ast.Load, ast.Store, ast.List, ast.Tuple)
    nodes = list(ast.walk(tree))
    if len(nodes) > 200 or any(not isinstance(node, permitted) for node in nodes):
        raise ResourceFetchError('unsupported_script', '仅支持简单赋值、字面量、列表及三个资源函数；不支持导入、循环、属性、索引或任意执行')
    functions = {'fetch_resource', 'extract_links', 'select_resources'}
    for node in nodes:
        if isinstance(node, ast.Name) and not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', node.id):
            raise ResourceFetchError('invalid_name', '资源脚本变量名无效')
        if isinstance(node, ast.Call) and (not isinstance(node.func, ast.Name) or node.func.id not in functions or any(kw.arg is None for kw in node.keywords)):
            raise ResourceFetchError('unsupported_call', '资源脚本只能调用三个固定资源函数')
        if isinstance(node, ast.Call):
            keys = [value.arg for value in node.keywords]
            expected = {'fetch_resource': set(), 'extract_links': {'kind'}, 'select_resources': {'limit'}}[node.func.id]
            if len(node.args) != 1 or set(keys) != expected or len(keys) != len(set(keys)):
                raise ResourceFetchError('invalid_arguments', '资源函数的参数结构不符合约定')
        if isinstance(node, ast.Assign) and (len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name) or node.targets[0].id in functions):
            raise ResourceFetchError('invalid_assignment', '只允许给普通变量赋值')
    variables, cache = {}, {}
    spent = calls = downloads = 0

    async def download(url, *, derived=None):
        nonlocal spent, downloads
        normalized = _url(url)
        if derived is None and normalized not in authorized:
            raise ResourceFetchError('url_not_authorized', '脚本地址不在本次明确公开来源范围内', 403)
        if normalized in cache:
            return cache[normalized]
        if downloads >= 8 or spent >= total_bytes:
            raise ResourceFetchError('script_budget', '本次资源下载数量或总字节预算已用尽', 413)
        downloads += 1
        raw, metadata = await fetch_public(normalized, max_bytes=min(max_bytes, total_bytes - spent))
        spent += len(raw)
        if spent > total_bytes:
            raise ResourceFetchError('script_budget', '本次资源总字节预算已用尽', 413)
        if derived is not None:
            metadata = {**metadata, 'discovered_from': public_url(derived.source), 'resource_kind': derived.kind}
        resource = _Resource(raw, metadata)
        cache[normalized] = resource
        return resource

    async def evaluate(node, depth=0):
        nonlocal calls
        if depth > 12:
            raise ResourceFetchError('script_limit', '资源表达式嵌套过深')
        if isinstance(node, ast.Constant):
            if type(node.value) not in (str, int) or (isinstance(node.value, str) and len(node.value) > 8192):
                raise ResourceFetchError('unsupported_literal', '仅支持文本或整数参数')
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in variables:
                raise ResourceFetchError('unknown_variable', '脚本引用了未定义变量')
            return variables[node.id]
        if isinstance(node, (ast.List, ast.Tuple)):
            if len(node.elts) > max_resources:
                raise ResourceFetchError('script_limit', '资源列表过长')
            return [await evaluate(item, depth + 1) for item in node.elts]
        if not isinstance(node, ast.Call):
            raise ResourceFetchError('unsupported_script', '资源脚本包含不支持的表达式')
        calls += 1
        if calls > 8:
            raise ResourceFetchError('script_budget', '资源脚本最多执行八次资源函数')
        args = [await evaluate(value, depth + 1) for value in node.args]
        kwargs = {value.arg: await evaluate(value.value, depth + 1) for value in node.keywords}
        if len(kwargs) != len(node.keywords):
            raise ResourceFetchError('invalid_arguments', '资源函数不能重复指定参数')
        name = node.func.id
        if name == 'fetch_resource':
            if len(args) != 1 or kwargs or not isinstance(args[0], str):
                raise ResourceFetchError('invalid_arguments', 'fetch_resource需要一个明确的公开URL')
            return await download(args[0])
        if name == 'extract_links':
            if len(args) != 1 or not isinstance(args[0], _Resource) or set(kwargs) != {'kind'} or kwargs['kind'] not in {'image', 'video', 'file'}:
                raise ResourceFetchError('invalid_arguments', 'extract_links需要已下载页面及kind=image/video/file')
            page = args[0]
            if page.metadata['mime_type'] not in {'text/html', 'application/xhtml+xml'} or len(page.raw) > 2 * 1024 * 1024:
                raise ResourceFetchError('not_html', '仅从不超过2MiB的HTML页面提取资源链接')
            parser = _LinkParser(page.metadata['final_url'], kwargs['kind'])
            try:
                parser.feed(page.raw.decode('utf-8', errors='replace'))
                parser.close()
            except (ValueError, RecursionError):
                raise ResourceFetchError('invalid_html', '页面无法安全提取资源链接') from None
            return _Links(tuple(parser.links))
        if len(args) != 1 or not isinstance(args[0], _Links) or set(kwargs) != {'limit'} or type(kwargs['limit']) is not int or not 1 <= kwargs['limit'] <= max_resources:
            raise ResourceFetchError('invalid_arguments', 'select_resources需要已提取链接及有效limit')
        if not args[0].values:
            raise ResourceFetchError('no_resources', '页面没有找到符合类型的公开资源链接', 404)
        return [await download(link.url, derived=link) for link in args[0].values[:kwargs['limit']]]

    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            variables[statement.targets[0].id] = await evaluate(statement.value)
        elif isinstance(statement, ast.Expr):
            await evaluate(statement.value)
        else:
            raise ResourceFetchError('unsupported_script', '资源脚本包含不支持的语句')
    result = variables.get('result')
    values = result if isinstance(result, list) else [result]
    if not 1 <= len(values) <= max_resources or any(not isinstance(value, _Resource) for value in values):
        raise ResourceFetchError('missing_result', '脚本必须把已下载资源或资源列表赋给result')
    unique, seen = [], set()
    for resource in values:
        key = (resource.metadata['sha256'], resource.metadata['final_url'])
        if key not in seen:
            seen.add(key)
            unique.append((resource.raw, resource.metadata))
    return unique
