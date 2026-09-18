"""独立服务器身份签名绑定实际 TLS 证书，公钥与证书续期解耦。"""
import base64
import fcntl
import json
import os
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlparse
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import APIRouter, HTTPException, Query, Request, Response
from .crypto import canonical, digest
from .db import uid

router=APIRouter(prefix='/api/v1/server',tags=['服务器身份'])
DOMAIN=b'homeai-server-identity:v1\n'


def certificate(app):
    loaded=getattr(app,'tls_leaf_certificate',None)
    cert=loaded() if loaded else x509.load_pem_x509_certificate(app.settings.identity_certificate_file.read_bytes())
    now=datetime.now(timezone.utc)
    if not cert.not_valid_before_utc<=now<cert.not_valid_after_utc:
        raise HTTPException(503,'服务器身份绑定的 TLS 证书已过期或尚未生效')
    public=cert.public_key()
    if isinstance(public,ec.EllipticCurvePublicKey):
        raw=public.public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
    elif isinstance(public,rsa.RSAPublicKey):
        raw=public.public_bytes(serialization.Encoding.DER,serialization.PublicFormat.PKCS1)
    else:raise HTTPException(503,'尚不支持此 TLS 公钥类型')
    return digest(cert.public_bytes(serialization.Encoding.DER)),digest(raw)


def record(app,namespace_anchor=None):
    root=app.settings.state_dir;root.mkdir(parents=True,exist_ok=True)
    path=root/'server-identity.enc'
    fd=os.open(root/'server-identity.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'r+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        changed=False
        if path.exists():
            if path.is_symlink():raise RuntimeError('服务器身份文件不能是符号链接')
            value=app.vault.open(path.read_text(),'server-identity')
        else:
            key=ec.generate_private_key(ec.SECP256R1())
            value={'server_id':uid(),'private_key':key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()}
            changed=True
        if namespace_anchor is not None and 'namespace_anchor' not in value:
            # 保留旧客户端的 TLS 公钥命名空间，证书换钥后不改变同步/提醒身份。
            value['namespace_anchor']=namespace_anchor;changed=True
        if changed:
            temporary=path.with_suffix('.tmp-'+secrets.token_hex(8))
            with os.fdopen(os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as stream:
                stream.write(app.vault.seal(value,'server-identity'));stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,path)
        return value


def identity(app):
    value=record(app)
    key=serialization.load_pem_private_key(value['private_key'].encode(),password=None)
    if not isinstance(key,ec.EllipticCurvePrivateKey) or not isinstance(key.curve,ec.SECP256R1):
        raise RuntimeError('服务器身份必须使用 P-256')
    return value['server_id'],key


def addresses(values):
    result=[]
    for value in values:
        parsed=urlparse(value)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.path not in ('','/') or parsed.query or parsed.fragment:
            raise ValueError('访问地址必须是无凭据的 HTTPS 根地址')
        _=parsed.port
        normalized=value.rstrip('/')
        if normalized not in result:result.append(normalized)
    if len(result)>8:raise ValueError('最多配置八个访问地址')
    return result


def configured_addresses(app):
    path=app.settings.state_dir/'pairing-address.enc'
    if path.exists():return addresses(app.vault.open(path.read_text(),'pairing-address')['addresses'])
    return addresses(app.settings.server_addresses)


def public_identity(app):
    fingerprint,anchor=certificate(app)
    value=record(app,anchor)
    server_id,key=identity(app)
    return {'server_id':server_id,'server_public_key':key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
            'namespace_anchor':value['namespace_anchor'],'fingerprint':fingerprint,'addresses':configured_addresses(app)}


@router.get('/identity')
def prove_identity(request:Request,response:Response,nonce:str=Query(pattern=r'^[0-9a-f]{64}$')):
    app=request.app.state
    try:public=public_identity(app)
    except (OSError,ValueError):raise HTTPException(503,'服务器身份或 TLS 证书配置不可用') from None
    _,key=identity(app)
    now=int(time.time())
    payload=canonical({'schema_version':'1.0',**public,'nonce':nonce,'issued_at':now,'expires_at':now+60})
    response.headers['Cache-Control']='no-store'
    return {'payload':base64.b64encode(payload).decode(),
            'signature':base64.b64encode(key.sign(DOMAIN+payload,ec.ECDSA(hashes.SHA256()))).decode()}
