"""DNS 验证仅使用绑定平台签发的租约，不接触 Cloudflare Token。"""
import base64
import secrets
import time
from urllib.parse import urlparse
import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from .remote import read_config,identity
from .acme_certificates import domain_name

class RemoteDNS:
    def __init__(self,app):
        self.app=app
        config=read_config(app)
        if not config or not config.get('enabled'):raise ValueError('需要已启用的官方远程绑定')
        self.instance=config['instance_id'];self.portal=config['portal_url'];self.domain=domain_name(urlparse(config['url']).hostname)
        self.lease=None
        self.authorize()

    def authorize(self):
        config=read_config(self.app)
        if not config or not config.get('enabled') or config['instance_id']!=self.instance or config['portal_url']!=self.portal or urlparse(config['url']).hostname!=self.domain:
            raise ValueError('远程绑定已变更或停用')
        endpoint=urlparse(self.portal)
        if endpoint.scheme!='https' or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:raise ValueError('平台地址无效')
        timestamp=int(time.time());nonce=secrets.token_hex(16);_,key=identity(self.app)
        signature=base64.b64encode(key.sign(f'{self.instance}\n{timestamp}\n{nonce}'.encode(),ec.ECDSA(hashes.SHA256()))).decode()
        with httpx.Client(timeout=20,trust_env=False,follow_redirects=False) as client:
            response=client.post(self.portal+'/api/agent/lease',json={'instance_id':self.instance,'credential':config['credential'],'lease':self.lease,'timestamp':timestamp,'nonce':nonce,'signature':signature})
            response.raise_for_status();lease=response.json()
            if lease['domain']!=self.domain:raise ValueError('平台租约域名不匹配')
            self.lease=lease['token']

    def _change(self,name,value,action):
        if name.rstrip('.')!='_acme-challenge.'+self.domain:raise ValueError('DNS 名称超出已绑定实例')
        self.authorize()
        with httpx.Client(timeout=20,trust_env=False,follow_redirects=False) as client:
            response=client.post(self.portal+'/api/agent/dns/'+action,json={'lease':self.lease,'value':value})
            response.raise_for_status()
            if response.json().get('name')!=name.rstrip('.'):raise ValueError('DNS 网关返回的名称不匹配')

    def present(self,name,value):
        self._change(name,value,'present')
        import dns.resolver
        resolver=dns.resolver.Resolver();resolver.timeout=3;resolver.lifetime=5
        deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            try:
                records=resolver.resolve(name,'TXT')
                if any(b''.join(record.strings).decode('ascii')==value for record in records):return
            except (dns.exception.DNSException,UnicodeError):pass
            time.sleep(2)
        raise TimeoutError('DNS 验证记录尚未传播，保留失败状态')
    def cleanup(self,name,value):self._change(name,value,'cleanup')
