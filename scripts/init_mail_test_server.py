"""只创建本机协议验收邮箱与 CA，不配置公网邮件或真实用户账号。"""
import base64
import hashlib
import json
import os
import secrets
from pathlib import Path
from datetime import datetime, timedelta, timezone
from cryptography import x509
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

root = Path(__file__).resolve().parents[1] / 'state' / 'mail-test'
root.mkdir(parents=True, exist_ok=True, mode=0o700)
if (root / 'credentials.json').exists():
    raise SystemExit('保留已有协议验收邮箱；不会覆盖凭据和证书')
(root / 'config').mkdir(exist_ok=True)
(root / 'tls').mkdir(exist_ok=True)


def write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as file:
        file.write(data)


now = datetime.now(timezone.utc)
ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Home AI local mail test CA')])
ca = x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=30)).add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True).add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False), critical=True).add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False).sign(ca_key, hashes.SHA256())
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
cert = x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])).issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=7)).add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'), x509.DNSName('mail.example.test')]), critical=False).add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True).add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False).add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False).sign(ca_key, hashes.SHA256())
write(root / 'tls' / 'ca.pem', ca.public_bytes(serialization.Encoding.PEM))
write(root / 'tls' / 'server.pem', cert.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM))
write(root / 'tls' / 'server.key', key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
password = secrets.token_urlsafe(32)
password_hash = '{SHA512}' + base64.b64encode(hashlib.sha512(password.encode()).digest()).decode()
write(root / 'config' / 'postfix-accounts.cf', ('alice@example.test|' + password_hash + '\n').encode())
write(root / 'config' / 'postfix-main.cf', b'default_transport = error:external delivery disabled\nrelay_transport = error:external delivery disabled\n')
write(root / 'credentials.json', json.dumps({'user': 'alice@example.test', 'password': password}).encode())
print('已创建独立测试邮箱与本地 CA；不输出密码，禁止公网转发。')
