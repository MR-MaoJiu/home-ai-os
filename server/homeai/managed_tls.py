"""通过新的 SSLContext 原子替换证书，身份端点读取同一份已加载叶证书。"""
import asyncio,json,logging,os,re,ssl
from dataclasses import dataclass
from datetime import datetime,timezone
from pathlib import Path
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from .certificate_store import selection
from .acme_certificates import inspect_certificate
from .remote import private_write


@dataclass(frozen=True)
class ActiveCertificate:
    context:ssl.SSLContext
    leaf:x509.Certificate
    identifier:str
    certificate_path:Path
    domain:str
    test_certificate:bool


class TLSManager:
    def __init__(self,app,test_ca=None):
        self.app=app;self.test_ca=test_ca;self.active=None;self.last_attempt=None;self.last_error=None;self.server=None;self.stopped=False

    def load(self,chosen):
        if self.test_ca and self.app.settings.environment=='production':raise ValueError('生产模式不能启用测试 CA')
        identifier=chosen['bundle_id']
        if not re.fullmatch(r'[0-9a-f]{32}',identifier):raise ValueError('证书包标识无效')
        path=self.app.settings.state_dir/'tls/bundles'/identifier
        if path.is_symlink():raise ValueError('证书快照不能是符号链接')
        # 快照不再位于 ACME 暂存目录；独立验证内容和信任链，不依赖文件名。
        for name in ('server.key','fullchain.pem','manifest.json'):
            if (path/name).is_symlink():raise ValueError('证书包不能包含符号链接')
        if (path/'server.key').stat().st_mode&0o077:raise ValueError('证书私钥权限不安全')
        metadata=json.loads((path/'manifest.json').read_text())
        chain=(path/'fullchain.pem').read_bytes();key=(path/'server.key').read_bytes()
        checked=inspect_certificate(chain,key,chosen['domain'])
        if checked['fingerprint']!=chosen['fingerprint'] or metadata['fingerprint']!=chosen['fingerprint'] or metadata['domain']!=chosen['domain']:
            raise ValueError('已选择证书内容发生变化')
        if bool(chosen['test_certificate'])!=bool(self.test_ca):raise ValueError('证书信任模式不匹配')
        from cryptography.x509.verification import PolicyBuilder,Store
        import certifi
        roots=x509.load_pem_x509_certificates(Path(self.test_ca or certifi.where()).read_bytes())
        certificates=x509.load_pem_x509_certificates(chain)
        PolicyBuilder().store(Store(roots)).time(datetime.now(timezone.utc)).build_server_verifier(x509.DNSName(chosen['domain'])).verify(certificates[0],certificates[1:])
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2
        context.options|=ssl.OP_NO_TICKET;context.num_tickets=0
        context.load_cert_chain(str(path/'fullchain.pem'),str(path/'server.key'))
        return ActiveCertificate(context,certificates[0],identifier,path/'fullchain.pem',chosen['domain'],chosen['test_certificate'])

    def write_status(self,error=None):
        active=self.active;now=datetime.now(timezone.utc)
        valid=bool(active and active.leaf.not_valid_before_utc<=now<active.leaf.not_valid_after_utc)
        listening=bool(self.server and self.server.started and not self.server.should_exit and not self.stopped)
        phase='stopped' if self.stopped else ('starting' if not listening else ('expired' if not valid else ('error' if error else 'active')))
        value={'status':phase,'checked_at':now.isoformat(),'listening':listening,
               'listen_host':self.server.config.host if self.server else None,'listen_port':self.server.config.port if self.server else None,
               'error_type':type(error).__name__ if error else None,'active_bundle':active.identifier if active else None,
               'fingerprint':active.leaf.fingerprint(hashes.SHA256()).hex() if active else None,
               'expires_at':active.leaf.not_valid_after_utc.isoformat() if active else None,
               'domain':active.domain if active else None,'test_certificate':active.test_certificate if active else None,
               'valid':valid}
        private_write(self.app.settings.state_dir/'tls-runtime.enc',self.app.vault.seal(value,'tls-runtime'))

    def sni(self,socket,server_name,initial_context):
        active=self.active;now=datetime.now(timezone.utc)
        if not active or not active.leaf.not_valid_before_utc<=now<active.leaf.not_valid_after_utc:
            raise ssl.SSLError('TLS certificate unavailable')
        socket.context=active.context

    async def watch(self):
        while True:
            try:
                chosen=selection(self.app)
                if chosen and chosen['bundle_id']!=self.last_attempt:
                    self.last_attempt=chosen['bundle_id']
                    candidate=await asyncio.to_thread(self.load,chosen)
                    self.active=candidate
                    self.app.settings.identity_certificate_file=candidate.certificate_path
                    self.last_error=None
                elif chosen and self.active and chosen['bundle_id']==self.active.identifier:
                    self.last_error=None
            except Exception as error:
                self.last_error=error
            try:self.write_status(self.last_error)
            except OSError as error:
                logging.error('TLS 状态无法保存：%s',type(error).__name__)
            await asyncio.sleep(2)


async def serve(app,host='127.0.0.1',port=58448,test_ca=None):
    if test_ca and (app.state.settings.environment=='production' or host not in {'127.0.0.1','localhost'}):
        raise ValueError('测试 CA 仅能用于本机开发服务')
    from .server_identity import record,public_identity
    if 'namespace_anchor' not in record(app.state):
        public_identity(app.state)
    manager=TLSManager(app.state,test_ca)
    chosen=selection(app.state)
    if not chosen:raise ValueError('请先选择经过验证的证书包')
    try:manager.active=manager.load(chosen)
    except Exception as error:
        status_file=app.state.settings.state_dir/'tls-runtime.enc'
        if not status_file.exists():raise
        previous=app.state.vault.open(status_file.read_text(),'tls-runtime')
        manager.active=manager.load({'bundle_id':previous['active_bundle'],'fingerprint':previous['fingerprint'],
            'domain':previous['domain'],'test_certificate':previous['test_certificate']})
        manager.last_error=error
    manager.last_attempt=chosen['bundle_id']
    app.state.settings.identity_certificate_file=manager.active.certificate_path
    app.state.tls_leaf_certificate=lambda:manager.active.leaf
    from starlette.responses import JSONResponse
    @app.middleware('http')
    async def certificate_lifetime(request,call_next):
        now=datetime.now(timezone.utc)
        if not manager.active.leaf.not_valid_before_utc<=now<manager.active.leaf.not_valid_after_utc:
            return JSONResponse({'detail':'HTTPS 证书已失效，请在服务器本机维护'},status_code=503)
        return await call_next(request)
    public_identity(app.state)
    # 独立选择器上下文不承载旧会话票据，每次握手选取当前完整上下文。
    selector=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);selector.minimum_version=ssl.TLSVersion.TLSv1_2
    selector.options|=ssl.OP_NO_TICKET;selector.num_tickets=0;selector.set_servername_callback(manager.sni)
    config=uvicorn.Config(app,host=host,port=port,access_log=False,
        ssl_certfile=str(manager.active.certificate_path),ssl_keyfile=str(manager.active.certificate_path.parent/'server.key'))
    config.load();config.ssl=selector
    server=uvicorn.Server(config)
    manager.server=server
    monitor=asyncio.create_task(manager.watch())
    try:await server.serve()
    finally:
        monitor.cancel()
        try:await monitor
        except asyncio.CancelledError:pass
        manager.stopped=True
        try:manager.write_status(manager.last_error)
        except OSError as error:logging.error('TLS 停止状态无法保存：%s',type(error).__name__)
