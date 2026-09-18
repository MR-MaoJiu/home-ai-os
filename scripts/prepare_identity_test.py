"""生成隔离库与真实 TLS 证书的身份轮换验收配置，不输出私钥或配对票据。"""
import json,os,secrets
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import ec
from homeai.api import create_app
from homeai.config import Settings
from homeai.db import Principal,uid
from homeai.security import credential
from homeai.server_identity import public_identity

root=Path('state/identity-qa')/secrets.token_hex(8);root.mkdir(parents=True,mode=0o700)
for label in ('original','renewed','mismatch'):
    key=ec.generate_private_key(ec.SECP256R1());name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')])
    cert=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc)-timedelta(minutes=1)).not_valid_after(datetime.now(timezone.utc)+timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]),critical=False).sign(key,hashes.SHA256())
    for suffix,data in [('key',key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())),('crt',cert.public_bytes(serialization.Encoding.PEM))]:
        with os.fdopen(os.open(root/(label+'.'+suffix),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as stream:stream.write(data)
s=Settings();s.database_url=s.database_url.rsplit('/',1)[0]+'/homeai_test';s.state_dir=root/'home';s.identity_certificate_file=root/'original.crt'
s.server_addresses=['https://localhost:58444','https://localhost:58445']
app=create_app(s).state;public=public_identity(app)
with app.db() as db:
    user=Principal(id=uid(),household_id=uid(),name='服务器身份验收',role='adult');db.add(user);db.flush()
    v2=credential(db,user.id,'pair',600);legacy=credential(db,user.id,'pair',600);db.commit()
value={'pairing':{'schema_version':'2.0',**public,'url':'https://localhost:58444','token':v2},
       'legacy':{'url':'https://localhost:58444','fingerprint':public['fingerprint'],'token':legacy},
       'renewed':'https://localhost:58445','mismatch':'https://localhost:58446','root':str(root.resolve())}
with os.fdopen(os.open(root/'fixture.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as stream:json.dump(value,stream)
print(str((root/'fixture.json').resolve()))
