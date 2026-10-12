"""HPKE及原Core鉴权集成；使用公开样例验证中继无法读取业务正文。"""
import base64
import json
import secrets
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric import ec,x25519
from sqlalchemy import select
from homeai.crypto import canonical
from homeai.db import Principal,Device,Task,scope,now
from homeai.relay_crypto import RelayKeys,seal,unseal,sign,verify,info,b64
from homeai.encrypted_relay import execute,RelayReceipt


def request(keys,client_key,device_id,method,target,headers,body=b''):
    advertisement=keys.advertisement()['payload'];identifier=secrets.token_hex(16)
    reply=x25519.X25519PrivateKey.generate()
    public=x25519.X25519PublicKey.from_public_bytes(base64.b64decode(advertisement['public_key']))
    clear={'metadata':{'wire_version':3,'kind':'request','id':identifier,'method':method,'target':target,'headers':headers,'size':len(body)},
        'body':b64(body),'response_public_key':b64(reply.public_key().public_bytes_raw())}
    enc,cipher=seal(public,clear,info('request',keys.server_id,identifier,advertisement['key_id']))
    payload={'version':1,'id':identifier,'server_id':keys.server_id,'key_id':advertisement['key_id'],'device_id':device_id,'expires':int(now())+120,'enc':enc,'ciphertext':cipher}
    return sign(client_key,b'homeai-relay-request:v1\n',payload),reply


def decrypted(keys,envelope,reply):
    value=verify(keys.identity.public_key(),b'homeai-relay-response:v1\n',envelope)
    clear=unseal(reply,value['enc'],value['ciphertext'],info('response',keys.server_id,value['id'],value['key_id']))
    return clear['status'],base64.b64decode(clear['body'])


@pytest.mark.asyncio
async def test_encrypted_request_retains_signed_http_auth_and_persistent_replay(system,alice):
    keys=RelayKeys(ec.generate_private_key(ec.SECP256R1()),'server-id','a'*64)
    raw=canonical({'capability':'reminder.create@v1','arguments':{'title':'PUBLIC-ENCRYPTION-TEST'},'idempotency_key':'relay-public-reminder'})
    path='/api/v1/tasks';headers=alice.headers('POST',path,raw)
    envelope,reply=request(keys,alice.key,alice.device_id,'POST',path,headers,raw)
    assert 'PUBLIC-ENCRYPTION-TEST' not in json.dumps(envelope) and alice.token not in json.dumps(envelope)
    response=await execute(system[0],keys,alice.device_id,envelope)
    status,data=decrypted(keys,response,reply)
    assert status==202
    identifier=json.loads(data)['id']
    repeated=await execute(system[0],keys,alice.device_id,envelope)
    assert repeated==response
    with system[2]() as db:
        scope(db,alice.user_id,'h1');rows=list(db.scalars(select(Task)))
        assert len(rows)==1 and rows[0].id==identifier
        receipt=db.get(RelayReceipt,envelope['payload']['id'])
        assert 'PUBLIC-ENCRYPTION-TEST' not in receipt.response


@pytest.mark.asyncio
async def test_process_restart_with_unknown_result_does_not_dispatch_again(system,alice):
    keys=RelayKeys(ec.generate_private_key(ec.SECP256R1()),'server-id','a'*64)
    envelope,reply=request(keys,alice.key,alice.device_id,'GET','/api/v1/me',alice.headers('GET','/api/v1/me',b''))
    await execute(system[0],keys,alice.device_id,envelope)
    with system[2]() as db:
        scope(db,alice.user_id,'h1');row=db.get(RelayReceipt,envelope['payload']['id']);row.response=None;db.commit()
    restarted=RelayKeys(keys.identity,keys.server_id,keys.namespace)
    response=await execute(system[0],restarted,alice.device_id,envelope)
    assert decrypted(keys,response,reply)[0]==409


@pytest.mark.asyncio
async def test_device_revocation_denies_even_cached_response(system,alice):
    keys=RelayKeys(ec.generate_private_key(ec.SECP256R1()),'server-id','a'*64)
    envelope,_=request(keys,alice.key,alice.device_id,'GET','/api/v1/me',alice.headers('GET','/api/v1/me',b''))
    await execute(system[0],keys,alice.device_id,envelope)
    with system[2]() as db:db.get(Device,alice.device_id).revoked=True;db.commit()
    with pytest.raises(PermissionError):await execute(system[0],keys,alice.device_id,envelope)


@pytest.mark.parametrize('change',['ciphertext','device','expired','external','namespace'])
def test_tamper_identity_expiry_and_context_are_rejected(change):
    keys=RelayKeys(ec.generate_private_key(ec.SECP256R1()),'server-id','a'*64);device=ec.generate_private_key(ec.SECP256R1())
    envelope,reply=request(keys,device,'paired-device','GET','/api/v1/me',{})
    if change=='ciphertext':envelope['payload']['ciphertext']='AAAA'
    if change=='device':
        with pytest.raises(Exception):keys.open_request(envelope,device.public_key(),'other-device')
        return
    if change=='expired':
        envelope['payload']['expires']=int(now())-1;envelope=sign(device,b'homeai-relay-request:v1\n',envelope['payload'])
    if change=='external':
        from homeai.direct_http import validate_request
        p=keys.open_request(envelope,device.public_key(),'paired-device')[1]['metadata'];p['target']='https://example.com/'
        with pytest.raises(ValueError):validate_request(p)
        return
    if change=='namespace':
        encrypted=keys.open_request(envelope,device.public_key(),'paired-device')[1]
        assert encrypted['metadata']['target']=='/api/v1/me'
        wrong=RelayKeys(keys.identity,'other-server','a'*64)
        with pytest.raises(Exception):wrong.open_request(envelope,device.public_key(),'paired-device')
        return
    with pytest.raises(Exception):keys.open_request(envelope,device.public_key(),'paired-device')

def test_relay_opt_in_is_household_scoped_and_preserved(system,alice):
    from homeai.remote import relay_enabled
    assert not relay_enabled(system[0].state)
    saved=alice.request('PUT','/api/v1/remote/stun',{'mode':'platform','stun_urls':[],'allow_encrypted_relay':True})
    assert saved.status_code==200
    assert relay_enabled(system[0].state,{'household_id':'h1'})
    assert not relay_enabled(system[0].state,{'household_id':'another-household'})
    assert alice.request('PUT','/api/v1/remote/stun',{'mode':'platform','stun_urls':[]}).status_code==200
    assert relay_enabled(system[0].state,{'household_id':'h1'})
    assert alice.request('PUT','/api/v1/remote/stun',{'mode':'platform','stun_urls':[],'allow_encrypted_relay':False}).status_code==200
    assert not relay_enabled(system[0].state,{'household_id':'h1'})


@pytest.mark.asyncio
async def test_agent_waits_for_capacity_before_claiming_another_request(system,monkeypatch):
    import asyncio
    from homeai import encrypted_relay as relay
    app=system[0];app.state.settings.encrypted_relay_enabled=True
    key=ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setattr(relay,'read_config',lambda _: {'enabled':True,'instance_id':'public-instance','credential':'isolated-test','portal_url':'https://platform.test'})
    monkeypatch.setattr(relay,'identity',lambda _:('public-server',key))
    monkeypatch.setattr(relay,'public_identity',lambda _:{'namespace_anchor':'a'*64})
    release=asyncio.Event();started=[];claimed=[]
    class Broker:
        def __init__(self,*args):pass
        async def request(self,method,path,value=None):
            await asyncio.sleep(0)
            if path=='/api/relay/requests':
                if len(claimed)>=5:
                    await asyncio.sleep(.01);return {'requests':[]}
                identifier=str(len(claimed)).zfill(32);claimed.append(identifier)
                return {'requests':[{'device_id':'public-device','envelope':{'payload':{'id':identifier}}}]}
            return {'accepted':True}
    async def execute(*args):
        started.append(args[-1]['payload']['id']);await release.wait();return {'public_result':'done'}
    monkeypatch.setattr(relay,'RelayBroker',Broker);monkeypatch.setattr(relay,'execute',execute)
    runner=asyncio.create_task(relay.run_agent(app))
    try:
        async with asyncio.timeout(3):
            while len(started)<4:await asyncio.sleep(.01)
        await asyncio.sleep(.3)
        assert len(claimed)==4,'执行位满时不能领取第五项后丢弃'
        release.set()
        async with asyncio.timeout(3):
            while len(started)<5:await asyncio.sleep(.01)
        assert len(set(started))==5
    finally:
        runner.cancel();await asyncio.gather(runner,return_exceptions=True)
