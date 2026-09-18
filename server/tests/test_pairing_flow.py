import base64,json
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes,serialization
from homeai.pairing import crypt
from test_server_identity import cert
from conftest import SignedClient


def test_member_qr_local_pair_once_and_household_isolation(system,alice,tmp_path):
    app=system[0].state
    app.settings.identity_certificate_file=tmp_path/'tls.pem';cert(app.settings.identity_certificate_file)
    for bad in ('http://example.test','https://localhost','https://127.0.0.1','https://example.test/path'):
        assert alice.request('PUT','/api/v1/pairing/address',{'url':bad}).status_code==422
    assert alice.request('PUT','/api/v1/pairing/address',{'url':'https://192.168.1.9:58443'}).status_code==200
    member=alice.request('POST','/api/v1/members',{'name':'家庭成员'}).json()['user_id']
    outsider=SignedClient(system[1],system[2],household='other',role='infrastructure_owner')
    assert outsider.request('POST','/api/v1/members/'+member+'/pairing').status_code==404
    response=alice.request('POST','/api/v1/members/'+member+'/pairing')
    assert response.status_code==200,response.text
    assert response.headers['cache-control']=='no-store'
    code=response.json()['code'];assert code['url']=='https://192.168.1.9:58443' and code['schema_version']=='2.0'
    key=ec.generate_private_key(ec.SECP256R1())
    body={'token':code['token'],'public_key':key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode(),'signature':base64.b64encode(key.sign(('homeai-pair:'+code['token']).encode(),ec.ECDSA(hashes.SHA256()))).decode(),'name':'测试手机'}
    assert system[1].post('/api/v1/pair',json=body).status_code==200
    assert system[1].post('/api/v1/pair',json=body).status_code==401


def test_enrollment_cipher_is_bound_to_ticket_direction_and_secret():
    encrypted=crypt('x'*43,'ticket',{'secret':'value'},'request')
    assert crypt('x'*43,'ticket',encrypted,'request',True)=={'secret':'value'}
    for token,identifier,direction in [('x'*43,'another','request'),('y'*43,'ticket','request'),('x'*43,'ticket','response')]:
        with pytest.raises(Exception):crypt(token,identifier,encrypted,direction,True)


def test_retired_manual_binding_endpoints_removed(system,alice):
    assert alice.request('GET','/api/v1/remote/identity').status_code==404
    assert alice.request('POST','/api/v1/remote/connect',{}).status_code==404
    assert alice.request('POST','/api/v1/members/invite',{'name':'unused'}).status_code==404


@pytest.mark.skipif(__import__('os').environ.get('HOMEAI_DIRECT_INTEGRATION')!='1',reason='需要真实 PostgreSQL RLS')
def test_pairing_audit_uses_postgres_rls_identity(tmp_path):
    from homeai.api import create_app
    from homeai.config import Settings
    from homeai.crypto import Vault
    from fastapi.testclient import TestClient
    import uuid
    path=tmp_path/'cert.pem';cert(path)
    s=Settings(state_dir=tmp_path,identity_certificate_file=path,server_addresses=['https://192.168.1.9:58443'])
    s.database_url=s.database_url.rsplit('/',1)[0]+'/homeai_test'
    app=create_app(s,Vault(b't'*32));client=SignedClient(TestClient(app),app.state.db,household=str(uuid.uuid4()),role='infrastructure_owner')
    try:
        response=client.request('POST','/api/v1/members/'+client.user_id+'/pairing')
        assert response.status_code==200,response.text
        assert client.request('PUT','/api/v1/remote/stun',{'mode':'custom','stun_urls':['stun:example.test:3478']}).status_code==200
    finally:client.request('DELETE','/api/v1/devices/'+client.device_id)
