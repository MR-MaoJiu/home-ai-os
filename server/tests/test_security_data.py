import pytest
from homeai.crypto import Vault
from homeai.db import Record
from conftest import SignedClient


def record(source_id='one',version=1,payload=None,kind='calendar.event'):
    return {'source':'ios','source_id':source_id,'kind':kind,'version':version,'payload':payload or {'title':'私人日程'}}


def put(client, data=None):
    response=client.request('POST','/api/v1/data/sync',{'records':[data or record()]})
    assert response.status_code == 200,response.text
    return response.json()['records'][0]['id']


def test_pair_and_replay(system,alice):
    headers=alice.headers('GET','/api/v1/me',b'')
    assert system[1].get('/api/v1/me',headers=headers).status_code==200
    assert system[1].get('/api/v1/me',headers=headers).status_code==401
    assert alice.request('DELETE','/api/v1/devices/'+alice.device_id).status_code==200
    assert alice.request('GET','/api/v1/me').status_code==401


def test_cross_user_and_household(system,alice):
    bob=SignedClient(system[1],system[2])
    outsider=SignedClient(system[1],system[2],household='h2')
    rid=put(alice)
    path='/api/v1/data/'+rid
    assert bob.request('GET',path).status_code==404
    assert alice.request('PUT',path+'/grants/'+outsider.user_id).status_code==404
    assert alice.request('PUT',path+'/grants/'+bob.user_id).status_code==200
    assert bob.request('GET',path).status_code==200
    assert bob.request('DELETE',path).status_code==404
    assert alice.request('DELETE',path+'/grants/'+bob.user_id).status_code==200
    assert bob.request('GET',path).status_code==404


def test_sync_versions_delete_no_resurrection(system,alice):
    rid=put(alice)
    assert put(alice)==rid
    assert alice.request('POST','/api/v1/data/sync',{'records':[record(payload={'title':'篡改'})]}).status_code==409
    put(alice,record(version=2,payload={'title':'新版'}))
    assert alice.request('POST','/api/v1/data/sync',{'records':[record()]}).status_code==409
    assert alice.request('DELETE','/api/v1/data/'+rid).status_code==200
    assert alice.request('GET','/api/v1/data/'+rid).status_code==404
    assert alice.request('POST','/api/v1/data/sync',{'records':[record(version=3)]}).status_code==409
    with system[2]() as db:
        assert db.get(Record,rid).payload == ''
    assert (system[0].state.settings.state_dir/'deletions.jsonl').exists()


def test_sensitive_cannot_downgrade(alice):
    item=record(kind='health.sleep')
    item.update(sensitivity='PUBLIC',cloud_policy='REDACT_AND_ALLOW')
    rid=put(alice,item)
    result=alice.request('GET','/api/v1/data/'+rid).json()
    assert result['sensitivity']=='SENSITIVE'
    assert result['cloud_policy']=='LOCAL_ONLY'


def test_encryption_context_bound():
    vault=Vault(b'1'*32)
    value=vault.seal({'private':'数据'},'alice')
    assert '数据' not in value
    assert vault.open(value,'alice')['private']=='数据'
    with pytest.raises(Exception):vault.open(value,'bob')


def test_signed_multipart_body_not_consumed_or_tampered(system, alice):
    request = alice.client.build_request('POST', '/api/v1/files', files={'file': ('note.txt', b'original document')})
    raw = request.read()
    headers = alice.headers('POST', '/api/v1/files', raw)
    headers['content-type'] = request.headers['content-type']
    changed = raw.replace(b'original document', b'modified document')
    response = alice.client.post('/api/v1/files', content=changed, headers=headers)
    assert response.status_code == 401
    headers = alice.headers('POST', '/api/v1/files', raw)
    headers['content-type'] = request.headers['content-type']
    response = alice.client.post('/api/v1/files', content=raw, headers=headers)
    assert response.status_code == 200, response.text


def test_nonfinite_signed_timestamp_is_rejected(alice):
    from homeai.crypto import digest
    for timestamp in ('nan','inf','-inf'):
        headers=alice.headers('GET','/api/v1/me',b'')
        headers['x-homeai-time']=timestamp
        proof='\n'.join([timestamp,headers['x-homeai-nonce'],'GET','/api/v1/me',digest(b''),digest(alice.token.encode())])
        headers['x-homeai-signature']=alice.sign(proof.encode())
        assert alice.client.get('/api/v1/me',headers=headers).status_code==401


def test_legacy_file_migration_preserves_classification_and_download(system,alice):
    import base64
    from homeai.crypto import digest
    from test_docling_live import upload
    data=b'original legacy document'
    rid=put(alice,{'source':'files','source_id':digest(data),'kind':'document.import','version':4,'sensitivity':'SECRET','payload':{'name':'legacy.txt','content_base64':base64.b64encode(data).decode()}})
    assert alice.request('GET','/api/v1/files/'+rid+'/content').content==data
    migrated=upload(alice,'legacy.txt',data)
    assert migrated==rid
    record=alice.request('GET','/api/v1/data/'+rid).json()
    assert record['version']==5 and record['sensitivity']=='SECRET' and record['kind']=='document.file'
    assert 'content_base64' not in record['payload']
    response=alice.request('GET','/api/v1/files/'+rid+'/content')
    assert response.content==data and response.headers['cache-control']=='no-store'
    assert alice.request('POST','/api/v1/files/'+rid+'/parse').status_code==403


def test_grant_management_requires_owner_and_revokes_visibility(system,alice):
    bob=SignedClient(system[1],system[2]);outsider=SignedClient(system[1],system[2],household='other')
    rid=put(bob,record(kind='health.sleep',payload={'value':1}))
    path='/api/v1/data/'+rid
    assert alice.request('GET',path).status_code==404
    assert alice.request('GET',path+'/grants').status_code==404
    assert bob.request('GET',path+'/grants').json()=={'grantee_ids':[]}
    assert bob.request('PUT',path+'/grants/'+alice.user_id).status_code==200
    assert alice.request('GET',path).status_code==200
    assert alice.request('GET',path+'/grants').status_code==404
    assert bob.request('GET',path+'/grants').json()['grantee_ids']==[alice.user_id]
    assert outsider.request('GET',path).status_code==404
    assert bob.request('DELETE',path+'/grants/'+alice.user_id).status_code==200
    assert alice.request('GET',path).status_code==404
