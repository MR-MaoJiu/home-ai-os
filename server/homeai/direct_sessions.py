"""家庭端直连会话：配对设备验证、持久化去重与有界连接生命周期。"""
import asyncio
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError

from .db import Device, Nonce, Principal, now
from .direct_http import serve_http
from .security import authenticate
from .server_identity import identity

router = APIRouter(prefix='/api/v1/direct', tags=['纯直连'])


class Offer(BaseModel):
    model_config = ConfigDict(extra='forbid')
    envelope: dict


@dataclass
class Session:
    peer: object
    device_id: str
    task: asyncio.Task | None = None
    source: str = "local"
    stop_reason: str | None = None


class DirectSessions:
    def __init__(self, app):
        self.app = app
        self.sessions = {}
        self.lock = asyncio.Lock()
        self.platform_expiry = 0

    async def negotiate(self, device_id, envelope, *, source="local", household_id=None):
        if not self.app.state.settings.direct_enabled:
            raise HTTPException(503, '纯直连入口尚未启用')
        from .direct_transport import DirectPeer
        with self.app.state.db() as db:
            device = db.get(Device, device_id)
            if not device or device.revoked or not db.get(Principal, device.user_id):
                raise HTTPException(401, '配对设备不可用')
            if household_id is not None and db.get(Principal, device.user_id).household_id != household_id:
                raise HTTPException(403, '设备不属于绑定家庭')
            trusted = serialization.load_pem_public_key(device.public_key.encode())
        _, key = identity(self.app.state)
        stun_urls = self.app.state.settings.direct_stun_urls
        if source == 'platform':
            from .remote import read_config
            config = read_config(self.app.state) or {}
            from .connect_signalling import effective_stun
            stun_urls = effective_stun(self.app.state,config)
        peer = DirectPeer(key, trusted, stun_urls=stun_urls)
        if source == 'platform':
            peer.authorized_until = lambda: self.platform_expiry
        session_id = None
        try:
            peer.ensure_authorized()
            verified = peer._verify(envelope, 'offer')
            session_id = verified['session']
            async with self.lock:
                if len(self.sessions) >= 32 or sum(s.device_id == device_id for s in self.sessions.values()) >= 4:
                    raise HTTPException(429, '直连会话数量达到上限')
                with self.app.state.db() as db:
                    db.add(Nonce(id='direct:' + device_id + ':' + session_id, expires_at=now() + 180))
                    try:
                        db.commit()
                    except IntegrityError:
                        raise HTTPException(409, '直连协商已使用') from None
                # 会话标识也在当前进程内唯一，防止不同设备互相覆盖。
                if session_id in self.sessions:
                    raise HTTPException(409, '直连会话已存在')
                self.sessions[session_id] = Session(peer, device_id, source=source)
            answer = await peer.answer(envelope)
            self.sessions[session_id].task = asyncio.create_task(self._serve(session_id))
            return answer
        except BaseException:
            if session_id and self.sessions.get(session_id) and self.sessions[session_id].peer is peer:
                self.sessions.pop(session_id)
            await peer.close()
            raise

    async def _serve(self, session_id):
        session = self.sessions[session_id]
        try:
            await serve_http(session.peer, self.app, session.device_id)
        except (Exception, asyncio.CancelledError) as error:
            import json
            from .private_files import private_write
            private_write(self.app.state.settings.state_dir/'direct-last-session.json', json.dumps({
                'ended_at':now(),'source':session.source,'reason':session.stop_reason or type(error).__name__,
                'progress':getattr(session.peer,'progress',None)}))
        finally:
            await session.peer.close()
            self.sessions.pop(session_id, None)

    async def close_source(self, source=None, reason="shutdown"):
        sessions = [s for s in self.sessions.values() if source is None or s.source == source]
        for session in sessions:
            session.stop_reason = reason
            if session.task:
                session.task.cancel()
        await asyncio.gather(*(s.task for s in sessions if s.task), return_exceptions=True)
        await asyncio.gather(*(s.peer.close() for s in sessions), return_exceptions=True)

    async def close(self):
        await self.close_source()
        self.sessions.clear()


def sessions(app):
    if not hasattr(app.state, 'direct_sessions'):
        app.state.direct_sessions = DirectSessions(app)
    return app.state.direct_sessions


@router.post('/offer')
async def offer(body: Offer, request: Request, actor=Depends(authenticate)):
    if not request.headers.get('authorization', '').startswith('Bearer '):
        raise HTTPException(403, '需要已配对设备的签名请求')
    try:
        return await sessions(request.app).negotiate(actor.device_id, body.envelope)
    except HTTPException:
        raise
    except ImportError:
        raise HTTPException(503, '尚未安装直连运行依赖') from None
    except Exception:
        raise HTTPException(422, '直连信令无效或无法协商') from None


@router.get('/task-snapshot')
def task_snapshot(request: Request, task_id: str | None = None, actor=Depends(authenticate)):
    from .crypto import digest
    from .task_events import snapshot
    if task_id:
        from uuid import UUID
        try:
            UUID(task_id)
        except ValueError:
            raise HTTPException(422, '任务标识无效') from None
    token = request.headers.get('authorization', '').removeprefix('Bearer ')
    return snapshot(request.app.state, actor, digest(token.encode()), task_id)


@router.get('/evidence')
def evidence(request: Request, actor=Depends(authenticate)):
    import ipaddress
    peer = request.scope.get('homeai.direct_peer')
    if not peer or not peer.pc.sctp:
        raise HTTPException(409, '此请求没有经过直连数据通道')
    # 固定 aiortc 1.15.0 的诊断字段，仅返回候选类型与地址分类，不公开 IP。
    pairs = peer.pc.sctp.transport.transport._connection._nominated.values()
    return {'transport': 'udp-dtls', 'connection': peer.pc.connectionState,
            'pairs': [{'local_type': pair.local_candidate.type, 'remote_type': pair.remote_candidate.type,
                       'remote_private': ipaddress.ip_address(pair.remote_candidate.host).is_private} for pair in pairs]}
