"""官方 ACME DNS-01 客户端：密钥留在家庭端，证书先暂存，不覆盖运行证书。"""
import base64
import fcntl
import ipaddress
import json
import logging
import os
import re
import secrets
from importlib.metadata import version
from datetime import datetime,timedelta,timezone
from pathlib import Path
from urllib.parse import urlparse
from acme import client,errors,messages
import josepy
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import ec,rsa
from .crypto import digest
from .remote import private_write

PRODUCTION='https://acme-v02.api.letsencrypt.org/directory'
STAGING='https://acme-staging-v02.api.letsencrypt.org/directory'


def domain_name(value):
    if not isinstance(value,str) or len(value)>253 or value!=value.lower() or len(value.split('.'))<2:
        raise ValueError('需要完整的小写 DNS 名称')
    if any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',label) for label in value.split('.')):
        raise ValueError('DNS 名称无效；不允许通配符')
    try:ipaddress.ip_address(value)
    except ValueError:return value
    raise ValueError('只支持 DNS 域名')


class RestrictedNetwork(client.ClientNetwork):
    def __init__(self,key,directory,ca_bundle=None):
        parsed=urlparse(directory)
        self.origin=(parsed.scheme,parsed.hostname,parsed.port or 443)
        if parsed.scheme!='https' or parsed.username is not None or parsed.password is not None:
            raise ValueError('ACME 目录必须使用 HTTPS')
        super().__init__(key=key,verify_ssl=str(ca_bundle) if ca_bundle else True,timeout=20,user_agent='HomeAI-ACME/1')
        self.session.trust_env=False
        self.order_endpoint=None
        self.on_order_created=None
    def _send_request(self,method,url,*args,**kwargs):
        parsed=urlparse(url)
        if (parsed.scheme,parsed.hostname,parsed.port or 443)!=self.origin or parsed.username is not None or parsed.password is not None:
            raise ValueError('ACME 响应包含未授权的端点')
        kwargs['allow_redirects']=False
        response=super()._send_request(method,url,*args,**kwargs)
        if method=='POST' and url==self.order_endpoint and response.status_code==201 and self.on_order_created:
            self.on_order_created(response.headers.get('Location'))
        return response
    def post(self,*args,**kwargs):
        # badNonce 明确表示请求未接受；仅对这一可判定拒绝进行有界重试。
        for attempt in range(3):
            try:return super().post(*args,**kwargs)
            except messages.Error as exc:
                if exc.code!='badNonce' or attempt==2:raise
                self._nonces.clear()


def inspect_certificate(chain_pem,key_pem,domain):
    certificates=x509.load_pem_x509_certificates(chain_pem)
    if len(certificates)<2:raise ValueError('CA 未返回完整证书链')
    leaf=certificates[0];key=serialization.load_pem_private_key(key_pem,password=None)
    public=lambda value:value.public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)
    if public(leaf.public_key())!=public(key.public_key()):raise ValueError('证书与家庭私钥不匹配')
    san=leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    names=san.get_values_for_type(x509.DNSName)
    if len(san)!=1 or names!=[domain]:raise ValueError('CA 返回的域名与申请不一致')
    try:
        if leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:raise ValueError('不能将 CA 证书作为家庭证书')
    except x509.ExtensionNotFound:pass
    now=datetime.now(timezone.utc)
    if not leaf.not_valid_before_utc<=now<leaf.not_valid_after_utc or leaf.not_valid_after_utc-now<timedelta(hours=1):
        raise ValueError('证书有效期不可用')
    for child,parent in zip(certificates,certificates[1:]):child.verify_directly_issued_by(parent)
    return {'domain':domain,'expires_at':leaf.not_valid_after_utc.isoformat(),
            'fingerprint':leaf.fingerprint(hashes.SHA256()).hex(),'chain_length':len(certificates)}


def issue(app,domain,publisher,*,directory=STAGING,agree_tos=False,ca_bundle=None,test_mode=False):
    if version('acme')!='5.8.0':raise RuntimeError('ACME SDK 版本未验收，请使用 5.8.0')
    domain=domain_name(domain)
    if not agree_tos:raise ValueError('必须明确接受所选 CA 的服务条款')
    if directory not in {PRODUCTION,STAGING}:
        parsed=urlparse(directory)
        if not test_mode or app.settings.environment=='production' or parsed.hostname not in {'localhost','127.0.0.1'} or not ca_bundle:
            raise ValueError('自定义测试 CA 仅允许本机 HTTPS 和显式 CA 证书')
    elif ca_bundle:raise ValueError('公共 CA 不允许替换系统信任根')
    logging.getLogger('acme').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    root=app.settings.state_dir/'acme';root.mkdir(parents=True,exist_ok=True)
    descriptor=os.open(root/'operation.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    with os.fdopen(descriptor,'r+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        label=digest(directory.encode());account_path=root/('account-'+label+'.enc')
        if account_path.exists():account=app.vault.open(account_path.read_text(),'acme-account:'+label)
        else:
            key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
            account={'key':key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode(),'directory':directory}
            private_write(account_path,app.vault.seal(account,'acme-account:'+label))
        key=josepy.JWKRSA(key=serialization.load_pem_private_key(account['key'].encode(),password=None))
        pending_path=root/'dns-pending.enc'
        operation_path=root/'operation.enc'
        operation=app.vault.open(operation_path.read_text(),'acme-operation') if operation_path.exists() else None
        if operation and (operation['domain']!=domain or operation['directory']!=directory):
            raise ValueError('存在另一域名或 CA 的未完成订单，必须先核对')
        if operation and operation['phase']=='SUBMITTING' and not operation.get('order_uri'):
            raise RuntimeError('新订单请求结果不明，需要核对 CA 账户订单；不会自动重新申请')
        def save_operation():private_write(operation_path,app.vault.seal(operation,'acme-operation'))
        net=RestrictedNetwork(key,directory,ca_bundle)
        try:
            ca=client.ClientV2(client.ClientV2.get_directory(directory,net),net)
            if account.get('registration'):
                net.account=messages.RegistrationResource.from_json(account['registration'])
            else:
                try:registration=ca.new_account(messages.NewRegistration(terms_of_service_agreed=True))
                except errors.ConflictError as exc:registration=ca.query_registration(messages.RegistrationResource(uri=exc.location,body=messages.Registration()))
                account['registration']=registration.to_json();private_write(account_path,app.vault.seal(account,'acme-account:'+label))
            # 上次崩溃留下的挑战必须先清理；不能遗忘不确定的 DNS 副作用。
            if pending_path.exists():
                pending=app.vault.open(pending_path.read_text(),'acme-dns-pending')
                if pending['domain']!=domain:raise ValueError('存在另一域名未清理的 DNS 挑战')
                publisher.cleanup(pending['name'],pending['value']);pending_path.unlink()
            if operation and operation['phase']=='STAGED':
                details=operation['result'];stored=Path(details['path'])
                inspect_certificate((stored/'fullchain.pem').read_bytes(),(stored/'server.key').read_bytes(),domain)
                operation_path.unlink();return details
            if operation is None:
                leaf_key=ec.generate_private_key(ec.SECP256R1())
                key_pem=leaf_key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
                csr=x509.CertificateSigningRequestBuilder().subject_name(x509.Name([])).add_extension(x509.SubjectAlternativeName([x509.DNSName(domain)]),critical=False).sign(leaf_key,hashes.SHA256())
                operation={'domain':domain,'directory':directory,'phase':'PREPARED','key':key_pem.decode(),
                           'csr':csr.public_bytes(serialization.Encoding.PEM).decode()}
                save_operation()
            key_pem=operation['key'].encode()
            if operation['phase']=='SUBMITTING' and not operation.get('order_uri'):
                raise RuntimeError('新订单请求结果不明，需要核对 CA 账户订单；不会自动重新申请')
            if operation.get('order_uri'):
                # 固定 SDK 的适配点：只查询原订单和授权，不创建替代订单。
                response=ca._post_as_get(operation['order_uri'])
                body=messages.Order.from_json(response.json())
                authzs=[ca._authzr_from_response(ca._post_as_get(url),uri=url) for url in body.authorizations]
                order=messages.OrderResource(body=body,uri=operation['order_uri'],authorizations=authzs,csr_pem=operation['csr'].encode())
                if body.status==messages.STATUS_INVALID:raise RuntimeError('原订单已失败，需要人工核对后重新申请')
            else:
                operation['phase']='SUBMITTING';save_operation()
                def remember_order(uri):
                    parsed=urlparse(uri or '')
                    if (parsed.scheme,parsed.hostname,parsed.port or 443)!=net.origin or parsed.username is not None:
                        raise RuntimeError('CA 未返回可信订单地址，需要核对')
                    operation.update(phase='ORDER_CREATED',order_uri=uri);save_operation()
                # 先落盘 201 响应的订单地址，再让 SDK 查询授权；授权查询失败仍可恢复。
                net.order_endpoint=ca.directory['newOrder'];net.on_order_created=remember_order
                order=ca.new_order(operation['csr'].encode())
                if not isinstance(order.uri,str) or not order.uri:
                    raise RuntimeError('CA 未返回订单地址，结果需要核对；不会自动重新申请')
                operation.update(phase='ORDER_CREATED',order_uri=order.uri);save_operation()
            challenge_record=None
            try:
                if len(order.authorizations)!=1:raise ValueError('仅允许单域名授权')
                authorization=order.authorizations[0]
                if authorization.body.identifier.value!=domain:raise ValueError('ACME 授权域名不匹配')
                if authorization.body.status!=messages.STATUS_VALID:
                    challenge=next((item for item in authorization.body.challenges if item.chall.typ=='dns-01'),None)
                    if challenge is None:raise ValueError('CA 未提供 DNS-01')
                    response,validation=challenge.response_and_validation(key)
                    name=challenge.chall.validation_domain_name(domain)
                    challenge_record={'domain':domain,'name':name,'value':validation}
                    private_write(pending_path,app.vault.seal(challenge_record,'acme-dns-pending'))
                    publisher.present(name,validation)
                    ca.answer_challenge(challenge,response)
                if order.body.status in {messages.STATUS_VALID,messages.STATUS_PROCESSING}:
                    completed=ca.poll_finalization(order,deadline=datetime.now()+timedelta(seconds=180))
                else:completed=ca.poll_and_finalize(order,deadline=datetime.now()+timedelta(seconds=180))
                chain=completed.fullchain_pem.encode()
                details=inspect_certificate(chain,key_pem,domain)
                directory_path=root/domain/secrets.token_hex(12);directory_path.mkdir(parents=True,mode=0o700)
                private_write(directory_path/'server.key',key_pem.decode())
                private_write(directory_path/'fullchain.pem',chain.decode())
                details.update(status='staged',directory=directory,order_uri=order.uri,test_certificate=directory!=PRODUCTION,
                               created_at=datetime.now(timezone.utc).isoformat(),path=str(directory_path))
                private_write(directory_path/'manifest.json',json.dumps(details))
                operation.update(phase='STAGED',result=details);save_operation()
            except Exception as failure:
                operation['error_type']=type(failure).__name__
                if isinstance(failure,messages.Error):operation['acme_error_code']=failure.code
                save_operation()
                raise
            finally:
                if challenge_record:
                    # 清理失败保留密文日志；下次操作先对账，不宣称整轮成功。
                    try:publisher.cleanup(challenge_record['name'],challenge_record['value'])
                    except Exception as cleanup_error:
                        operation['cleanup_error_type']=type(cleanup_error).__name__;save_operation()
                        raise
                    pending_path.unlink(missing_ok=True)
            operation_path.unlink()
            return details
        finally:net.session.close()
