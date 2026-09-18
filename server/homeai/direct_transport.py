"""签名 SDP 与仅直连的 WebRTC 数据通道；尚未接入平台信令或 iOS。"""
import asyncio
import base64
import hashlib
import ipaddress
import json
import secrets
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

DOMAIN = b'homeai-direct-sdp:v1\n'
MAX_FRAME = 16384
MAX_DESCRIPTION = 32768


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def validate_sdp(sdp):
    """仅允许已签名的 UDP 直连候选，不接受媒体、TURN 或追加候选。"""
    from aiortc.sdp import SessionDescription
    if not isinstance(sdp, str) or len(sdp.encode()) > MAX_DESCRIPTION:
        raise ValueError('连接描述超过限制')
    description = SessionDescription.parse(sdp)
    if len(description.media) != 1:
        raise ValueError('仅允许单个数据通道传输')
    media = description.media[0]
    if media.kind != 'application' or media.profile != 'UDP/DTLS/SCTP':
        raise ValueError('仅允许加密数据通道')
    if not media.dtls or not media.dtls.fingerprints:
        raise ValueError('缺少 DTLS 证书指纹')
    if not media.ice_candidates_complete or not 1 <= len(media.ice_candidates) <= 32:
        raise ValueError('必须提供完整且有界的直连候选')
    for candidate in media.ice_candidates:
        if candidate.type not in {'host', 'srflx'} or candidate.protocol.lower() != 'udp' or candidate.component != 1:
            raise ValueError('禁止中继或非 UDP 候选')
        address = ipaddress.ip_address(candidate.ip)
        if address.is_multicast or address.is_unspecified or not 1 <= candidate.port <= 65535:
            raise ValueError('候选地址无效')


class DirectPeer:
    """调用方必须从既有配对关系提供可信对端公钥，不能从信令中取信任根。"""

    def __init__(self, signing_key, trusted_peer_key, *, stun_urls=()):
        from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection
        for key in (signing_key, trusted_peer_key):
            if not isinstance(key.curve, ec.SECP256R1):
                raise ValueError('直连身份必须使用 P-256')
        if len(stun_urls) > 2 or any(not isinstance(url, str) or not url.startswith('stun:') or len(url) > 256 or any(c in url for c in '\r\n@?#') for url in stun_urls):
            raise ValueError('仅允许本机配置的 STUN 地址，禁止 TURN')
        # 显式空列表关闭库的默认公共 STUN；从不使用远端提供的 ICE 服务器。
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(urls=url) for url in stun_urls]))
        self.key, self.trusted = signing_key, trusted_peer_key
        self.session = None
        self.offer_digest = None
        self.accepted = False
        self.closed = False
        self.opened = asyncio.Event()
        self.writable = asyncio.Event()
        self.messages = asyncio.Queue(maxsize=16)
        self.channel = self.pc.createDataChannel('homeai', negotiated=True, id=0, protocol='homeai.direct.v1', ordered=True)
        self.channel.bufferedAmountLowThreshold = MAX_FRAME
        self.channel.on('open', self.opened.set)
        self.channel.on('bufferedamountlow', self.writable.set)
        self.channel.on('message', self._message)
        self.pc.on('datachannel', lambda channel: channel.close())

    def _message(self, value):
        if not isinstance(value, bytes) or len(value) > MAX_FRAME or self.messages.full():
            self.channel.close()
            return
        self.messages.put_nowait(value)

    def _sign(self, description):
        validate_sdp(description.sdp)
        payload = {'version': 1, 'session': self.session, 'type': description.type,
                   'sdp': description.sdp, 'expires': int(time.time()) + 60,
                   'offer_digest': self.offer_digest}
        signature = self.key.sign(DOMAIN + canonical(payload), ec.ECDSA(hashes.SHA256()))
        return {'payload': payload, 'signature': base64.b64encode(signature).decode()}

    def _verify(self, envelope, kind):
        if not isinstance(envelope, dict) or set(envelope) != {'payload', 'signature'} or len(canonical(envelope)) > MAX_DESCRIPTION + 4096:
            raise ValueError('信令信封无效')
        payload = envelope['payload']
        if not isinstance(payload, dict) or set(payload) != {'version', 'session', 'type', 'sdp', 'expires', 'offer_digest'}:
            raise ValueError('信令字段无效')
        self.trusted.verify(base64.b64decode(envelope['signature'], validate=True), DOMAIN + canonical(payload), ec.ECDSA(hashes.SHA256()))
        current = time.time()
        if payload['version'] != 1 or payload['type'] != kind or type(payload['expires']) is not int or not current < payload['expires'] <= current + 65:
            raise ValueError('信令已过期或协议不匹配')
        session = payload['session']
        if not isinstance(session, str) or len(session) != 64 or any(c not in '0123456789abcdef' for c in session):
            raise ValueError('会话标识无效')
        validate_sdp(payload['sdp'])
        return payload

    async def offer(self):
        if self.session is not None or self.closed:
            raise ValueError('每个连接只允许一次协商')
        self.session = secrets.token_hex(32)
        try:
            await asyncio.wait_for(self.pc.setLocalDescription(await self.pc.createOffer()), 15)
            envelope = self._sign(self.pc.localDescription)
            self.offer_digest = hashlib.sha256(canonical(envelope['payload'])).hexdigest()
            return envelope
        except BaseException:
            await self.close()
            raise

    async def answer(self, envelope):
        from aiortc import RTCSessionDescription
        if self.session is not None or self.closed:
            raise ValueError('每个连接只允许一次协商')
        payload = self._verify(envelope, 'offer')
        if payload['offer_digest'] is not None:
            raise ValueError('初始请求不能声明响应关联')
        self.session = payload['session']
        self.offer_digest = hashlib.sha256(canonical(payload)).hexdigest()
        try:
            await asyncio.wait_for(self.pc.setRemoteDescription(RTCSessionDescription(payload['sdp'], 'offer')), 15)
            await asyncio.wait_for(self.pc.setLocalDescription(await self.pc.createAnswer()), 15)
            self.accepted = True
            return self._sign(self.pc.localDescription)
        except BaseException:
            await self.close()
            raise

    async def accept(self, envelope):
        from aiortc import RTCSessionDescription
        if self.session is None or self.accepted or self.closed:
            raise ValueError('连接状态不允许响应')
        payload = self._verify(envelope, 'answer')
        if payload['session'] != self.session or payload['offer_digest'] != self.offer_digest:
            raise ValueError('响应不属于当前协商')
        self.accepted = True
        try:
            await asyncio.wait_for(self.pc.setRemoteDescription(RTCSessionDescription(payload['sdp'], 'answer')), 15)
        except BaseException:
            await self.close()
            raise

    async def send(self, value, timeout=15):
        if not isinstance(value, bytes) or len(value) > MAX_FRAME:
            raise ValueError('数据帧必须是最多 16 KiB 的字节串')
        async with asyncio.timeout(timeout):
            await self.opened.wait()
            while self.channel.bufferedAmount > MAX_FRAME * 4:
                self.writable.clear()
                await self.writable.wait()
            if self.closed or self.channel.readyState != 'open':
                raise ConnectionError('直连已关闭')
            self.channel.send(value)

    async def receive(self, timeout=15):
        return await asyncio.wait_for(self.messages.get(), timeout)

    async def close(self):
        self.closed = True
        self.opened.set()
        self.writable.set()
        await self.pc.close()
