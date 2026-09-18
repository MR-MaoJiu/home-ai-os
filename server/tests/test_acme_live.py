"""真实 Pebble CA 与 DNS-01，不能用 ALWAYS_VALID 跳过域名验证。"""
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest
from homeai.acme_certificates import issue,domain_name,inspect_certificate
from homeai.config import Settings
from homeai.crypto import Vault


def test_acme_domain_scope():
    assert domain_name('nas.homeai.test')=='nas.homeai.test'
    for value in ['*.homeai.test','127.0.0.1','a..test','NAS.test','-a.test','https://home.test']:
        with pytest.raises(ValueError):domain_name(value)


class PebbleDNS:
    def __init__(self):self.presented=[];self.cleaned=[]
    def present(self,name,value):
        with httpx.Client(trust_env=False,timeout=10) as client:
            r=client.post('http://127.0.0.1:51455/set-txt',json={'host':name+'.','value':value});r.raise_for_status()
        self.presented.append(name)
    def cleanup(self,name,value):
        with httpx.Client(trust_env=False,timeout=10) as client:
            r=client.post('http://127.0.0.1:51455/clear-txt',json={'host':name+'.'});r.raise_for_status()
        self.cleaned.append(name)


@pytest.mark.skipif(os.getenv('HOMEAI_ACME_TEST')!='1',reason='需要真实 Pebble CA 和 DNS 挑战服务')
def test_real_dns01_issue_renewal_and_private_storage(tmp_path):
    app=SimpleNamespace(settings=Settings(state_dir=tmp_path),vault=Vault(os.urandom(32)))
    publisher=PebbleDNS();domain='nas-'+uuid.uuid4().hex[:12]+'.homeai.test'
    arguments={'directory':'https://localhost:51400/dir','ca_bundle':Path('state/vendor/pebble/test/certs/pebble.minica.pem'),
               'test_mode':True,'agree_tos':True}
    first=issue(app,domain,publisher,**arguments)
    assert first['status']=='staged' and first['test_certificate'] is True
    root=Path(first['path']);assert root.joinpath('server.key').stat().st_mode&0o077==0
    assert inspect_certificate(root.joinpath('fullchain.pem').read_bytes(),root.joinpath('server.key').read_bytes(),domain)['domain']==domain
    assert publisher.presented and publisher.cleaned==publisher.presented
    assert not (tmp_path/'acme/dns-pending.enc').exists()
    account=next((tmp_path/'acme').glob('account-*.enc'))
    assert 'PRIVATE KEY' not in account.read_text()
    second=issue(app,domain,publisher,**arguments)
    assert second['fingerprint']!=first['fingerprint']
    assert len(list((tmp_path/'acme').glob('account-*.enc')))==1
    assert not (tmp_path/'tls/server.crt').exists()
    with pytest.raises(ValueError):issue(app,'different.homeai.test',publisher,**{**arguments,'agree_tos':False})


@pytest.mark.skipif(os.getenv('HOMEAI_ACME_TEST')!='1',reason='需要真实测试 CA/DNS')
def test_existing_order_resumes_after_real_http_failure(tmp_path):
    app=SimpleNamespace(settings=Settings(state_dir=tmp_path),vault=Vault(os.urandom(32)))
    domain='resume-'+uuid.uuid4().hex[:12]+'.homeai.test'
    args={'directory':'https://localhost:51400/dir','ca_bundle':Path('state/vendor/pebble/test/certs/pebble.minica.pem'),'test_mode':True,'agree_tos':True}
    class InterruptedDNS(PebbleDNS):
        def present(self,name,value):
            super().present(name,value)
            with httpx.Client(trust_env=False) as client:
                client.post('http://127.0.0.1:51455/missing-confirmation').raise_for_status()
    with pytest.raises(httpx.HTTPStatusError):issue(app,domain,InterruptedDNS(),**args)
    pending=app.vault.open((tmp_path/'acme/operation.enc').read_text(),'acme-operation')
    assert pending['phase']=='ORDER_CREATED'
    recovered=issue(app,domain,PebbleDNS(),**args)
    assert recovered['order_uri']==pending['order_uri']
    assert not (tmp_path/'acme/operation.enc').exists()
    from homeai.private_files import private_write
    private_write(tmp_path/'acme/operation.enc',app.vault.seal({'phase':'SUBMITTING','domain':domain,'directory':args['directory']},'acme-operation'))
    with pytest.raises(RuntimeError,match='不会自动重新申请'):issue(app,domain,PebbleDNS(),**args)


@pytest.mark.skipif(os.getenv('HOMEAI_ACME_TEST')!='1',reason='需要真实测试 CA/DNS')
def test_cleanup_failure_reuses_staged_certificate(tmp_path):
    import json
    app=SimpleNamespace(settings=Settings(state_dir=tmp_path),vault=Vault(os.urandom(32)))
    domain='cleanup-'+uuid.uuid4().hex[:12]+'.homeai.test'
    args={'directory':'https://localhost:51400/dir','ca_bundle':Path('state/vendor/pebble/test/certs/pebble.minica.pem'),'test_mode':True,'agree_tos':True}
    class CleanupFailure(PebbleDNS):
        def cleanup(self,name,value):
            with httpx.Client(trust_env=False) as client:
                client.post('http://127.0.0.1:51455/missing-cleanup').raise_for_status()
    with pytest.raises(httpx.HTTPStatusError) as cleanup_error:issue(app,domain,CleanupFailure(),**args)
    assert cleanup_error.value.__context__ is None, type(cleanup_error.value.__context__).__name__
    assert (tmp_path/'acme/dns-pending.enc').exists()
    saved=json.loads(next((tmp_path/'acme'/domain).glob('*/manifest.json')).read_text())
    recovered=issue(app,domain,PebbleDNS(),**args)
    assert recovered['fingerprint']==saved['fingerprint'] and recovered['path']==saved['path']
    assert not (tmp_path/'acme/dns-pending.enc').exists()
