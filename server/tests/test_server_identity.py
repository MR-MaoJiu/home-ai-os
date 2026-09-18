"""真实密码学签名、证书绑定和并发身份初始化，不使用签名替身。"""
import base64
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from homeai.api import create_app
from homeai.config import Settings
from homeai.crypto import Vault
from homeai.server_identity import DOMAIN,identity,record,addresses


def cert(path,expired=False):
    key=ec.generate_private_key(ec.SECP256R1());name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')]);now=datetime.now(timezone.utc)
    certificate=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=2)).not_valid_after(now+timedelta(days=-1 if expired else 1)).sign(key,hashes.SHA256())
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return certificate


def test_identity_signature_certificate_rotation_and_expiry(tmp_path):
    path=tmp_path/'tls.crt';first=cert(path)
    app=create_app(Settings(state_dir=tmp_path,identity_certificate_file=path),Vault(b'i'*32),db_factory=lambda:None)
    client=TestClient(app);nonce='ab'*32
    response=client.get('/api/v1/server/identity',params={'nonce':nonce})
    assert response.status_code==200,response.text
    assert response.headers['cache-control']=='no-store'
    envelope=response.json();raw=base64.b64decode(envelope['payload']);payload=json.loads(raw)
    key=serialization.load_pem_public_key(payload['server_public_key'].encode())
    key.verify(base64.b64decode(envelope['signature']),DOMAIN+raw,ec.ECDSA(hashes.SHA256()))
    assert payload['nonce']==nonce and payload['fingerprint']==first.fingerprint(hashes.SHA256()).hex()
    assert payload['expires_at']-payload['issued_at']==60
    assert 'private_key' not in raw.decode()
    assert 'PRIVATE KEY' not in (tmp_path/'server-identity.enc').read_text()
    second=cert(path)
    rotated=json.loads(base64.b64decode(client.get('/api/v1/server/identity',params={'nonce':'cd'*32}).json()['payload']))
    assert rotated['fingerprint']==second.fingerprint(hashes.SHA256()).hex()!=payload['fingerprint']
    for field in ('server_id','server_public_key','namespace_anchor'):assert rotated[field]==payload[field]
    cert(path,expired=True)
    assert client.get('/api/v1/server/identity',params={'nonce':nonce}).status_code==503
    assert client.get('/api/v1/server/identity',params={'nonce':'invalid'}).status_code==422


def test_concurrent_identity_initialization_keeps_one_key(tmp_path):
    from types import SimpleNamespace
    app=SimpleNamespace(settings=Settings(state_dir=tmp_path),vault=Vault(b'i'*32))
    def read(_):
        identifier,key=identity(app)
        return identifier,key.public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)
    with ThreadPoolExecutor(max_workers=8) as executor:result=list(executor.map(read,range(30)))
    assert len(set(result))==1
    before=record(app)
    migrated=record(app,'a'*64)
    assert before['server_id']==migrated['server_id'] and before['private_key']==migrated['private_key']
    assert record(app,'b'*64)['namespace_anchor']=='a'*64


def test_identity_addresses_are_explicit_https_origins():
    import pytest
    assert addresses(['https://localhost:58443/','https://localhost:58443'])==['https://localhost:58443']
    for value in ('http://localhost','https://user:pass@example.test','https://example.test/path','https://example.test/#fragment','https://example.test/?token=secret'):
        with pytest.raises(ValueError):addresses([value])
