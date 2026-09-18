"""验证并选择不可变 TLS 证书包；选择不代表 HTTPS 服务已经切换。"""
import fcntl,json,os,re,secrets,ssl
from datetime import datetime,timezone
from pathlib import Path
import certifi
from cryptography import x509
from cryptography.x509.verification import PolicyBuilder,Store
from .acme_certificates import PRODUCTION,inspect_certificate
from .private_files import private_write


def validate_bundle(app,bundle,test_ca=None):
    root=(app.settings.state_dir/'acme').resolve()
    bundle=Path(bundle)
    if bundle.is_symlink() or not bundle.resolve().is_relative_to(root):raise ValueError('证书必须来自当前家庭的 ACME 暂存目录')
    bundle=bundle.resolve(strict=True)
    for name in ('manifest.json','fullchain.pem','server.key'):
        path=bundle/name
        if path.is_symlink() or not path.is_file():raise ValueError('证书包文件无效')
    if (bundle/'server.key').stat().st_mode&0o077:raise ValueError('证书私钥必须为 0600')
    metadata=json.loads((bundle/'manifest.json').read_text())
    chain=(bundle/'fullchain.pem').read_bytes();key=(bundle/'server.key').read_bytes()
    details=inspect_certificate(chain,key,metadata['domain'])
    if metadata.get('fingerprint')!=details['fingerprint']:raise ValueError('证书与暂存清单不一致')
    if test_ca:
        if app.settings.environment=='production':raise ValueError('生产模式不能启用测试 CA')
        roots=x509.load_pem_x509_certificates(Path(test_ca).read_bytes())
    else:
        if metadata.get('test_certificate') is not False or metadata.get('directory')!=PRODUCTION:
            raise ValueError('正式安装只接受已标明正式 CA 的证书')
        roots=x509.load_pem_x509_certificates(Path(certifi.where()).read_bytes())
    certificates=x509.load_pem_x509_certificates(chain)
    PolicyBuilder().store(Store(roots)).time(datetime.now(timezone.utc)).build_server_verifier(x509.DNSName(details['domain'])).verify(certificates[0],certificates[1:])
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(bundle/'fullchain.pem'),str(bundle/'server.key'))
    return metadata,details


def select_bundle(app,bundle,test_ca=None):
    root=app.settings.state_dir/'acme';root.mkdir(parents=True,exist_ok=True)
    descriptor=os.open(root/'operation.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    with os.fdopen(descriptor,'r+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if (root/'operation.enc').exists() or (root/'dns-pending.enc').exists():raise ValueError('ACME 订单或 DNS 清理尚未完成')
        _,details=validate_bundle(app,bundle,test_ca)
        identifier=secrets.token_hex(16)
        snapshot=app.settings.state_dir/'tls/bundles'/identifier
        snapshot.mkdir(parents=True,mode=0o700)
        bundle=Path(bundle)
        for name in ('fullchain.pem','server.key','manifest.json'):
            private_write(snapshot/name,(bundle/name).read_text())
        selection={'bundle_id':identifier,**details,'test_certificate':bool(test_ca),'selected_at':datetime.now(timezone.utc).isoformat(),'operator_uid':os.getuid()}
        private_write(snapshot/'selection.enc',app.vault.seal(selection,'tls-selection:'+identifier))
        private_write(app.settings.state_dir/'tls-selection.enc',app.vault.seal(selection,'tls-selection'))
        return {**selection,'status':'selected','requires_managed_https':True}


def selection(app):
    path=app.settings.state_dir/'tls-selection.enc'
    return app.vault.open(path.read_text(),'tls-selection') if path.exists() else None


def rollback_bundle(app,identifier,test_ca=None):
    if not re.fullmatch(r'[0-9a-f]{32}',identifier):raise ValueError('证书包标识无效')
    from .managed_tls import TLSManager
    root=app.settings.state_dir/'acme';root.mkdir(parents=True,exist_ok=True)
    with os.fdopen(os.open(root/'operation.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600),'r+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        snapshot=app.settings.state_dir/'tls/bundles'/identifier
        value=app.vault.open((snapshot/'selection.enc').read_text(),'tls-selection:'+identifier)
        if value['bundle_id']!=identifier:raise ValueError('历史证书选择不匹配')
        TLSManager(app,test_ca).load(value)
        value.update(selected_at=datetime.now(timezone.utc).isoformat(),operator_uid=os.getuid())
        private_write(app.settings.state_dir/'tls-selection.enc',app.vault.seal(value,'tls-selection'))
        return {**value,'status':'selected','rollback':True,'requires_managed_https':True}


def runtime_status(app):
    path=app.settings.state_dir/'tls-runtime.enc'
    if not path.exists():return {'status':'not_running','listening':False}
    value=app.vault.open(path.read_text(),'tls-runtime')
    checked=datetime.fromisoformat(value['checked_at'])
    age=(datetime.now(timezone.utc)-checked).total_seconds()
    if age < -5 or age > 10:
        value.update(status='stale',listening=False)
    return value
