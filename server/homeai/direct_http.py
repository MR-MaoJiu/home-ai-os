"""把有界 HTTP 请求送入同一个 Core 应用；不开放任意地址代理。"""
import asyncio
import json
import re
import secrets
import time

import httpx

from .direct_transport import MAX_FRAME

MAX_BODY = 30 * 1024 * 1024
ACK = b'\x00homeai-ack-v1'
WINDOW = 64
BODY_FRAME = 1024
HEADERS = {'authorization', 'content-type', 'x-homeai-time', 'x-homeai-nonce', 'x-homeai-signature'}


async def send_packet(peer, metadata, body):
    if len(body) > MAX_BODY:
        raise ValueError('直连请求或响应超过限制')
    await peer.send(json.dumps({**metadata, 'wire_version': 3, 'size': len(body)}, separators=(',', ':')).encode())
    for index, offset in enumerate(range(0, len(body), BODY_FRAME)):
        await peer.send(body[offset:offset + BODY_FRAME])
        if (index + 1) % WINDOW == 0 or offset + BODY_FRAME >= len(body):
            if await peer.receive(timeout=45) != ACK:
                raise ValueError('直连分帧确认不匹配')


async def receive_packet(peer, header=None):
    metadata = json.loads(await peer.receive(timeout=60) if header is None else header)
    received_at = time.time()
    if not isinstance(metadata, dict) or type(metadata.get('wire_version')) is not int or metadata['wire_version'] != 3 or type(metadata.get('size')) is not int or not 0 <= metadata['size'] <= MAX_BODY:
        raise ValueError('直连报文长度无效')
    body = bytearray()
    frames = 0
    async with asyncio.timeout(120):
        while len(body) < metadata['size']:
            frame = await peer.receive(timeout=45)
            if not frame or len(frame) > BODY_FRAME or len(frame) > metadata['size'] - len(body):
                raise ValueError('直连报文分帧无效')
            body.extend(frame)
            frames += 1
            peer.progress = {'phase':'receiving','bytes':len(body),'total':metadata['size']}
            if frames % WINDOW == 0 or len(body) == metadata['size']:
                await peer.send(ACK)
    return metadata, bytes(body), received_at


def validate_request(metadata):
    if set(metadata) != {'kind', 'id', 'method', 'target', 'headers', 'size', 'wire_version'} or metadata['kind'] != 'request' or type(metadata['wire_version']) is not int or metadata['wire_version'] != 3:
        raise ValueError('直连请求字段无效')
    if not isinstance(metadata['id'], str) or not re.fullmatch('[0-9a-f]{32}', metadata['id']):
        raise ValueError('直连请求标识无效')
    if metadata['method'] not in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'}:
        raise ValueError('不允许该 HTTP 方法')
    target = metadata['target']
    if not isinstance(target, str) or len(target) > 4096 or not re.fullmatch(r'/api/v1/[A-Za-z0-9_%/?=&.+:\-]*', target):
        raise ValueError('只允许 Core API 路径')
    url = httpx.URL('https://homeai.direct' + target)
    if url.raw_path.decode() != target or not url.path.startswith('/api/v1/') or url.path in {'/api/v1/pair'} or url.path.startswith('/api/v1/browser'):
        raise ValueError('直连路径无效，配对与网页登录必须使用原入口')
    headers = metadata['headers']
    if not isinstance(headers, dict) or set(headers) - HEADERS:
        raise ValueError('只允许设备签名请求头')
    if any(not isinstance(v, str) or len(v) > 4096 or '\r' in v or '\n' in v for v in headers.values()):
        raise ValueError('直连请求头无效')
    return target, headers


class DirectHTTPClient:
    """每个通道顺序执行；异常时关闭，不自动重放可能有副作用的请求。"""
    def __init__(self, peer):
        self.peer = peer
        self.lock = asyncio.Lock()

    async def request(self, method, target, *, headers, body=b''):
        async with self.lock:
            metadata = {'wire_version': 3, 'kind': 'request', 'id': secrets.token_hex(16), 'method': method,
                        'target': target, 'headers': headers, 'size': len(body)}
            validate_request(metadata)
            try:
                async with asyncio.timeout(180):
                    await send_packet(self.peer, metadata, body)
                    response, data, _ = await receive_packet(self.peer)
                    if response.get('kind') != 'response' or response.get('id') != metadata['id'] or type(response.get('status')) is not int or not 100 <= response['status'] <= 599:
                        raise ValueError('直连响应不匹配')
                    return response, data
            except BaseException:
                await self.peer.close()
                raise


async def serve_http(peer, app, device_id):
    """仍执行 Core 原有鉴权；通道身份不能替代每个请求的设备签名。"""
    async def bound_app(scope, receive, send):
        scope['homeai.direct_device_id'] = device_id
        scope['homeai.direct_peer'] = peer
        scope['homeai.received_at'] = received_at
        count = 0
        async def limited_send(message):
            nonlocal count
            if message['type'] == 'http.response.body':
                count += len(message.get('body', b''))
                if count > MAX_BODY:
                    raise ValueError('直连响应超过限制')
            await send(message)
        await app(scope, receive, limited_send)

    transport = httpx.ASGITransport(app=bound_app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url='https://homeai.direct', follow_redirects=False, trust_env=False) as client:
            await asyncio.wait_for(peer.opened.wait(),timeout=60)
            while True:
                # 空闲不是传输失败。等待下一请求期间仍检查许可和设备撤销。
                try:
                    header = await peer.receive(timeout=15)
                except TimeoutError:
                    from .db import Device
                    peer.ensure_authorized()
                    with app.state.db() as db:
                        device = db.get(Device, device_id)
                        if not device or device.revoked:
                            raise PermissionError('设备已撤销')
                    continue
                # 超时限制属于单次请求，不应成为整条长连接的寿命。
                async with asyncio.timeout(180):
                    metadata, body, received_at = await receive_packet(peer, header=header)
                    target, headers = validate_request(metadata)
                    peer.ensure_authorized()
                    response = await client.request(metadata['method'], target, headers=headers, content=body)
                    await send_packet(peer, {'kind': 'response', 'id': metadata['id'], 'status': response.status_code,
                                            'headers': {'content-type': response.headers.get('content-type', 'application/octet-stream')}}, response.content)

    finally:
        await peer.close()
