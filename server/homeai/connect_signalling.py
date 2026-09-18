"""官方平台仅用于签名信令交换；这里不提供业务数据转发接口。"""
import asyncio
import base64
import hashlib
import json
import math
import secrets
import time
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from .private_files import private_write
from .remote import read_config
from .server_identity import identity


DEFAULT_PORTAL = 'https://homeai-connect.pintheworld.cn'


def platform_origin(value):
    """协调平台必须是明确的 HTTPS 根地址，不接受家庭 API 路径。"""
    if not isinstance(value, str) or not value or len(value) > 300 or any(c.isspace() for c in value) or '\\' in value:
        raise ValueError('连接平台地址无效')
    parsed = urlparse(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('连接平台必须是无凭据的 HTTPS 根地址')
    # 主动校验非法或超范围端口，避免延迟到网络请求才失败。
    if parsed.port == 0:
        raise ValueError('连接平台端口无效')
    return value.rstrip('/')


def validate_capabilities(value):
    if not isinstance(value, dict) or type(value.get('protocol')) is not int or value['protocol'] != 1 or value.get('transport_policy') != 'direct_only':
        raise ValueError('平台不兼容 Home AI 纯直连协议 v1')
    return {'protocol': 1, 'transport_policy': 'direct_only', 'stun_urls': validate_stun_urls(value.get('stun_urls', []))}


async def inspect_platform(url):
    """公开能力探测不发送家庭身份、凭据或配对信息。"""
    origin = platform_origin(url)
    async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
        async with client.stream('GET', origin+'/api/direct/capabilities') as response:
            response.raise_for_status()
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 16384:
                    raise ValueError('平台能力响应超过限制')
    return {'portal_url': origin, **validate_capabilities(json.loads(raw))}


def validate_stun_urls(urls):
    if not isinstance(urls,list) or len(urls)>2:raise ValueError('最多两个 STUN 地址')
    for value in urls:
        if not isinstance(value,str) or not value.startswith('stun:') or len(value)>256 or any(c.isspace() for c in value) or any(c in value for c in '@?#\\'):
            raise ValueError('只允许 STUN 地址，禁止 TURN')
        parsed=urlparse('stun://'+value[5:])
        if not parsed.hostname or parsed.path or parsed.port==0:raise ValueError('STUN 地址无效')
    return urls


def effective_stun(app,config):
    path=app.settings.state_dir/'remote-network.enc'
    prefs=app.vault.open(path.read_text(),'remote-network') if path.exists() else {}
    if prefs.get('household_id') not in (None,config.get('household_id')):raise ValueError('网络配置家庭不匹配')
    if prefs.get('mode')=='custom':return validate_stun_urls(prefs.get('stun_urls',[]))
    return validate_stun_urls(config.get('stun_urls',app.settings.direct_stun_urls))


class Broker:
    def __init__(self, app, config):
        self.app, self.config = app, config
        self.base = platform_origin(config['portal_url'])
        if config.get('transport_policy') != 'direct_only' or config.get('protocol') != 1:
            raise ValueError('需要迁移到纯直连绑定')

    async def request(self, method, path, body=None):
        raw = json.dumps(body, separators=(',', ':')).encode() if body is not None else b''
        if len(raw) > 40000 or not path.startswith('/api/direct/') or '?' in path:
            raise ValueError('仅允许受限连接信令')
        _, key = identity(self.app)
        timestamp, nonce = str(int(time.time())), secrets.token_hex(16)
        proof = '\n'.join(['homeai-connect-direct:v1', 'agent', self.config['instance_id'], timestamp, nonce, method, path, hashlib.sha256(raw).hexdigest()])
        headers = {'content-type': 'application/json', 'x-connect-id': self.config['instance_id'],
                   'x-connect-credential': self.config['credential'], 'x-connect-time': timestamp, 'x-connect-nonce': nonce,
                   'x-connect-signature': base64.b64encode(key.sign(proof.encode(), ec.ECDSA(hashes.SHA256()))).decode()}
        async with httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False) as client:
            async with client.stream(method, self.base+path, content=raw, headers=headers) as response:
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 650000:
                        raise ValueError('信令平台响应超过限制')
                return json.loads(content)


async def run_agent(app):
    from .direct_sessions import sessions
    cache = {}
    previous = None
    permit_until = 0
    application_check = 0
    manager = sessions(app)
    try:
        while True:
            try:
                if time.time()-application_check>=10:
                    application_check=time.time()
                    try:
                        from .service_application import poll_application
                        await poll_application(app.state)
                    except Exception:
                        pass
                config = read_config(app.state)
                if not config or not config.get('enabled') or config.get('transport_policy') != 'direct_only':
                    manager.platform_expiry = 0
                    await manager.close_source('platform',reason='disabled')
                    cache.clear(); previous = None; permit_until = 0
                    private_write(app.state.settings.state_dir/'direct-status.json', json.dumps({'state':'disabled','checked_at':time.time()}))
                    await asyncio.sleep(2)
                    continue
                stamp = (config['instance_id'], config['credential'], config['portal_url'],tuple(effective_stun(app.state,config)))
                if previous != stamp:
                    manager.platform_expiry = 0
                    await manager.close_source('platform',reason='configuration_changed')
                    cache.clear(); previous = stamp; permit_until = 0
                broker = Broker(app.state, config)
                if permit_until <= time.time()+60:
                    permit = await broker.request('GET','/api/direct/permit')
                    expiry = float(permit['expires'])
                    if not math.isfinite(expiry) or not time.time() < expiry <= time.time()+305:
                        raise ValueError('连接许可期限无效')
                    permit_until = expiry
                    manager.platform_expiry = expiry
                from .pairing import process_enrollment
                try:
                    pending = await broker.request('GET','/api/direct/enrollments')
                except httpx.HTTPStatusError as error:
                    if error.response.status_code != 404:raise
                    pending = []
                if not isinstance(pending,list) or len(pending)>16:raise ValueError('配对队列无效')
                for item in pending:
                    try:
                        ciphertext=await process_enrollment(app.state,config,item)
                        await broker.request('POST','/api/direct/enrollments/'+item['id']+'/response',{'ciphertext':ciphertext})
                    except Exception as error:
                        # 仅保留错误类型，不记录请求、配对码或会话。
                        private_write(app.state.settings.state_dir/'enrollment-status.json',json.dumps({'state':'failed','error_type':type(error).__name__,'checked_at':time.time()}))
                        continue
                offers = await broker.request('GET', '/api/direct/offers')
                if not isinstance(offers, list) or len(offers) > 16:
                    raise ValueError('信令队列格式无效')
                cache = {k: v for k, v in cache.items() if v[0] > time.time()}
                for item in offers:
                    envelope = item['envelope']
                    session_id = envelope['payload']['session']
                    if session_id not in cache:
                        try:
                            answer = await manager.negotiate(item['device_id'], envelope, source='platform', household_id=config['household_id'])
                        except Exception:
                            # 不重放失败的协商；客户端到期后创建新会话，不涉及业务重试。
                            cache[session_id] = (envelope['payload']['expires'], None)
                            continue
                        cache[session_id] = (envelope['payload']['expires'], answer)
                    if cache[session_id][1] is not None:
                        await broker.request('POST', '/api/direct/answers/'+session_id, {'envelope': cache[session_id][1]})
                private_write(app.state.settings.state_dir/'direct-status.json', json.dumps({'state': 'signalling_online', 'checked_at': time.time()}))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                denied = isinstance(error,httpx.HTTPStatusError) and error.response.status_code in {401,403}
                if denied or time.time() >= permit_until:
                    cache.clear()
                    manager.platform_expiry = 0
                    await manager.close_source('platform',reason='authorization_denied' if denied else 'permit_expired')
                private_write(app.state.settings.state_dir/'direct-status.json', json.dumps({'state': 'signalling_unavailable', 'error_type': type(error).__name__, 'checked_at': time.time()}))
            await asyncio.sleep(2)
    finally:
        await manager.close_source('platform')
