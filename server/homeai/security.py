import base64
import math
import secrets
from dataclasses import dataclass
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException, Request
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import ec
from .crypto import digest
from .db import Credential, Device, Principal, Nonce, now, scope


@dataclass(frozen=True)
class Actor:
    user_id: str
    household_id: str
    device_id: str
    role: str


def verify(public_key: str, signature: str, message: bytes):
    try:
        key = serialization.load_pem_public_key(public_key.encode())
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError("不支持的设备密钥")
        key.verify(base64.b64decode(signature, validate=True), message, ec.ECDSA(hashes.SHA256()))
    except Exception:
        raise HTTPException(401, "设备签名无效") from None


def credential(db, user_id: str, kind: str, seconds: int, device_id=None):
    token = secrets.token_urlsafe(32)
    db.add(Credential(digest=digest(token.encode()), user_id=user_id, device_id=device_id, kind=kind, expires_at=now() + seconds))
    return token


async def authenticate(request: Request):
    if request.cookies.get("__Host-homeai-session") and not request.headers.get("authorization"):
        from .browser_auth import get_browser_actor
        return get_browser_actor(request)
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    timestamp = request.headers.get("x-homeai-time", "")
    nonce = request.headers.get("x-homeai-nonce", "")
    signature = request.headers.get("x-homeai-signature", "")
    try:
        parsed_time = float(timestamp)
        received_at = request.scope.get("homeai.received_at", now())
        if not math.isfinite(parsed_time) or abs(now() - received_at) > 180 or abs(received_at - parsed_time) > 60 or not 16 <= len(nonce) <= 100:
            raise ValueError()
    except ValueError:
        raise HTTPException(401, "请求证明缺失或已过期") from None
    with request.app.state.db() as db:
        auth = db.get(Credential, digest(token.encode()))
        if not auth or auth.expires_at <= now() or (auth.kind != "access" and not (auth.kind == "refresh" and request.url.path == "/api/v1/session/renew")):
            raise HTTPException(401, "会话无效或已过期")
        bound_device = request.scope.get('homeai.direct_device_id')
        if bound_device is not None and auth.device_id != bound_device:
            raise HTTPException(401, '请求凭据与直连设备不匹配')
        device = db.get(Device, auth.device_id)
        user = db.get(Principal, auth.user_id)
        if not device or device.revoked or not user or device.user_id != user.id:
            raise HTTPException(401, "设备已撤销")
        body_digest = request.scope.get("homeai.body_digest")
        if body_digest is None:
            body_digest = digest(await request.body())
        path = request.url.path + ("?" + request.url.query if request.url.query else "")
        proof = "\n".join([timestamp, nonce, request.method, path, body_digest, digest(token.encode())])
        verify(device.public_key, signature, proof.encode())
        db.add(Nonce(id=device.id + ":" + nonce, expires_at=now() + 120))
        try:
            db.commit()
        except IntegrityError:
            raise HTTPException(401, "请求证明已使用") from None
        return Actor(user.id, user.household_id, device.id, user.role)


def own(db, model, item_id: str, actor: Actor):
    scope(db, actor.user_id, actor.household_id)
    item = db.scalar(select(model).where(model.id == item_id, model.owner_id == actor.user_id, model.household_id == actor.household_id))
    if not item:
        raise HTTPException(404, "资源不存在")
    return item


def owner(actor):
    if actor.role != "infrastructure_owner":
        raise HTTPException(403, "需要基础设施管理权限")
