"""真实 CA 签发证书，通过真实 HTTPS 验证上下文切换、身份连续性与失败保留。"""
import base64,hashlib,http.client,json,os,socket,ssl,subprocess,time,uuid
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest
from cryptography import x509
from homeai.acme_certificates import issue
from homeai.certificate_store import select_bundle,rollback_bundle
from homeai.config import Settings
from homeai.crypto import Vault
from homeai.remote import private_write
from test_acme_live import PebbleDNS

pytestmark=pytest.mark.skipif(os.getenv('HOMEAI_ACME_TEST')!='1',reason='需要真实测试 CA、DNS、HTTPS 与已配置测试数据库')


def read_identity(port,domain,root_ca,sni=True):
    context=ssl.create_default_context(cafile=str(root_ca))
    if not sni:context.check_hostname=False
    with socket.create_connection(('127.0.0.1',port),timeout=5) as tcp:
        with context.wrap_socket(tcp,server_hostname=domain if sni else None) as channel:
            der=channel.getpeercert(binary_form=True)
            # 无 SNI 分支仍验证 CA 链，再手工核对本例唯一 DNS SAN。
            certificate=x509.load_der_x509_certificate(der)
            assert certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)==[domain]
            channel.sendall(f'GET /api/v1/server/identity?nonce={"12"*32} HTTP/1.1\r\nHost: {domain}\r\nConnection: close\r\n\r\n'.encode())
            response=http.client.HTTPResponse(channel);response.begin();assert response.status==200
            payload=json.loads(base64.b64decode(json.loads(response.read())['payload']))
            assert payload['fingerprint']==hashlib.sha256(der).hexdigest()
            return payload


def test_real_https_switch_and_invalid_candidate_keeps_old(tmp_path):
    key=os.urandom(32);master=tmp_path/'master.key';master.write_bytes(key);master.chmod(0o600)
    settings=Settings(state_dir=tmp_path,master_key_file=master)
    app=SimpleNamespace(settings=settings,vault=Vault(key))
    domain='switch-'+uuid.uuid4().hex[:10]+'.homeai.test'
    options={'directory':'https://localhost:51400/dir','ca_bundle':Path('state/vendor/pebble/test/certs/pebble.minica.pem'),'test_mode':True,'agree_tos':True}
    first=issue(app,domain,PebbleDNS(),**options)
    with httpx.Client(verify=ssl.create_default_context(cafile=str(options['ca_bundle'])),trust_env=False) as client:
        response=client.get('https://localhost:51401/roots/0');response.raise_for_status()
    ca_file=tmp_path/'root.pem';ca_file.write_bytes(response.content)
    with pytest.raises(ValueError):select_bundle(app,first['path'])
    settings.environment='production'
    with pytest.raises(ValueError):select_bundle(app,first['path'],ca_file)
    settings.environment='development'
    selected=select_bundle(app,first['path'],ca_file)
    # 运行包独立于暂存目录，清理原私钥不会损坏已选快照。
    (Path(first['path'])/'server.key').unlink()
    with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    legacy=tmp_path/'local.crt';legacy.write_bytes((Path(first['path'])/'fullchain.pem').read_bytes())
    legacy_bytes=legacy.read_bytes()
    env=os.environ.copy();env.update(HOMEAI_STATE_DIR=str(tmp_path),HOMEAI_MASTER_KEY_FILE=str(master),HOMEAI_IDENTITY_CERTIFICATE_FILE=str(legacy),
        HOMEAI_DATABASE_URL=settings.database_url.rsplit('/',1)[0]+'/homeai_test',HOMEAI_ENVIRONMENT='development')
    log=(tmp_path/'https.log').open('w')
    process=subprocess.Popen(['.venv/bin/python','scripts/run_managed_api.py','--port',str(port),'--test-ca',str(ca_file)],env=env,stdout=log,stderr=subprocess.STDOUT)
    try:
        for _ in range(50):
            if process.poll() is not None:raise AssertionError((tmp_path/'https.log').read_text())
            try:before=read_identity(port,domain,ca_file);break
            except (OSError,AssertionError):time.sleep(.2)
        else:raise AssertionError('HTTPS 未就绪')
        assert before['fingerprint']==first['fingerprint']
        second=issue(app,domain,PebbleDNS(),**options)
        next_selected=select_bundle(app,second['path'],ca_file)
        for _ in range(30):
            after=read_identity(port,domain,ca_file)
            if after['fingerprint']==second['fingerprint']:break
            time.sleep(.2)
        assert after['fingerprint']==second['fingerprint']!=before['fingerprint']
        assert after['server_id']==before['server_id'] and after['namespace_anchor']==before['namespace_anchor']
        assert read_identity(port,domain,ca_file,sni=False)['fingerprint']==second['fingerprint']
        # 将损坏候选作为期望状态，验证运行器也会独立拒绝，而非只依赖选择命令。
        broken=dict(next_selected,bundle_id='f'*32)
        private_write(tmp_path/'tls-selection.enc',app.vault.seal(broken,'tls-selection'))
        time.sleep(3)
        assert read_identity(port,domain,ca_file)['fingerprint']==second['fingerprint']
        status=app.vault.open((tmp_path/'tls-runtime.enc').read_text(),'tls-runtime')
        assert status['status']=='error' and status['valid'] and status['active_bundle']==next_selected['bundle_id']
        assert process.poll() is None
        assert legacy.read_bytes()==legacy_bytes
        process.terminate();process.wait(timeout=10)
        process=subprocess.Popen(['.venv/bin/python','scripts/run_managed_api.py','--port',str(port),'--test-ca',str(ca_file)],env=env,stdout=log,stderr=subprocess.STDOUT)
        for _ in range(50):
            if process.poll() is not None:raise AssertionError((tmp_path/'https.log').read_text())
            try:
                recovered=read_identity(port,domain,ca_file)
                break
            except (OSError,AssertionError):time.sleep(.2)
        assert recovered['fingerprint']==second['fingerprint']
        rollback_bundle(app,selected['bundle_id'],ca_file)
        for _ in range(30):
            reverted=read_identity(port,domain,ca_file)
            if reverted['fingerprint']==first['fingerprint']:break
            time.sleep(.2)
        assert reverted['fingerprint']==first['fingerprint']
        assert reverted['server_id']==before['server_id']
    finally:
        process.terminate()
        try:process.wait(timeout=10)
        except subprocess.TimeoutExpired:process.kill();process.wait()
        log.close()
