"""真实 PostgreSQL 上的隔离直连验收服务，仅监听本机。"""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
import uvicorn
from urllib.parse import urlparse
from datetime import datetime,timedelta,timezone
import ipaddress
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from homeai.api import create_app
from homeai.config import Settings
from homeai.crypto import Vault
from homeai.db import Principal, uid
from homeai.private_files import private_write
from homeai.security import credential
from homeai.server_identity import public_identity

root = Path('state/direct-native')
root.mkdir(parents=True, exist_ok=True)
keyfile = root / 'master.key'
if not keyfile.exists():
    with os.fdopen(os.open(keyfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as stream:
        stream.write(os.urandom(32))
url = os.environ.get('HOMEAI_DIRECT_TEST_URL', 'https://localhost:58544')
hostname = urlparse(url).hostname
tls = root/'tls'; tls.mkdir(exist_ok=True)
tls_key = ec.generate_private_key(ec.SECP256R1()) if not (tls/'server.key').exists() else serialization.load_pem_private_key((tls/'server.key').read_bytes(), password=None)
if not (tls/'server.key').exists():
    with os.fdopen(os.open(tls/'server.key',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as stream:
        stream.write(tls_key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
try:
    extra = x509.IPAddress(ipaddress.ip_address(hostname))
except ValueError:
    extra = x509.DNSName(hostname)
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Home AI Direct Test')])
certificate = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(tls_key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc)-timedelta(minutes=1)).not_valid_after(datetime.now(timezone.utc)+timedelta(days=2)).add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),x509.IPAddress(ipaddress.ip_address('127.0.0.1')),extra]),critical=False).sign(tls_key,hashes.SHA256())
# 只在首次启动生成证书；准备新票据时不能改变运行服务已加载的证书。
if not (tls/'server.crt').exists():
    (tls/'server.crt').write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
settings = Settings(state_dir=root, master_key_file=keyfile, direct_enabled=True, identity_certificate_file=tls/'server.crt')
settings.database_url = settings.database_url.rsplit('/', 1)[0] + '/homeai_test'
app = create_app(settings)
if '--reuse-fixture' not in sys.argv:
    with app.state.db() as db:
        from homeai.remote import read_config
        remote = read_config(app.state)
        user = Principal(id=uid(), household_id=remote.get('household_id', uid()) if remote else uid(), name='原生直连验收', role='adult')
        db.add(user)
        token = credential(db, user.id, 'pair', 300)
        db.commit()
    private_write(root/'fixture-user.json', json.dumps({'user_id':user.id,'household_id':user.household_id}))
    public = public_identity(app.state)
    private_write(root/'pairing.json', json.dumps({'pairing': {'schema_version': '2.0', **public,
        'url': url, 'token': token}}, ensure_ascii=False))
print('复用已有隔离验收身份。' if '--reuse-fixture' in sys.argv else '隔离直连配对文件已就绪，有效期五分钟。', flush=True)
if '--prepare-only' in sys.argv:
    raise SystemExit(0)
uvicorn.run(app, host=os.environ.get('HOMEAI_DIRECT_TEST_LISTEN','127.0.0.1'), port=58544, ssl_keyfile=str(tls/'server.key'), ssl_certfile=str(tls/'server.crt'), access_log=False)
