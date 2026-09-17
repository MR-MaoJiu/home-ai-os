"""生成本项目本机开发证书；不修改系统信任设置。"""
import ipaddress
import os
from datetime import datetime,timedelta,timezone
from pathlib import Path
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import ec
from homeai.crypto import digest

root=Path(__file__).resolve().parents[1]/'state/tls'
root.mkdir(parents=True,exist_ok=True)
if not (root/'server.key').exists():
    key=ec.generate_private_key(ec.SECP256R1())
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Home AI Development')])
    cert=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc)-timedelta(minutes=1)).not_valid_after(datetime.now(timezone.utc)+timedelta(days=90)).add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False).sign(key,hashes.SHA256())
    fd=os.open(root/'server.key',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as f:f.write(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    (root/'server.crt').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
else:cert=x509.load_pem_x509_certificate((root/'server.crt').read_bytes())
(root/'fingerprint.txt').write_text(digest(cert.public_bytes(serialization.Encoding.DER)))
print('开发 TLS 证书已就绪，指纹保存在 state/tls/fingerprint.txt')
