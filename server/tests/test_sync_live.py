"""真实 PostgreSQL 的分页、共享撤权、批次重试与设备隔离验证。"""
import os,uuid
import pytest
from fastapi.testclient import TestClient
from homeai.api import create_app
from homeai.config import Settings
from homeai.db import Principal,SyncSnapshot,scope
from conftest import SignedClient
from test_security_data import put

pytestmark=pytest.mark.skipif(os.environ.get('HOMEAI_INTEGRATION')!='1',reason='需要真实 PostgreSQL 同步迁移')

@pytest.fixture
def clients():
    settings=Settings();settings.database_url=settings.database_url.rsplit('/',1)[0]+'/homeai_test'
    app=create_app(settings);client=TestClient(app);household=str(uuid.uuid4())
    return app.state,SignedClient(client,app.state.db,household=household),SignedClient(client,app.state.db,household=household)


def initialize(user):
    result=user.request('POST','/api/v1/sync/snapshot');assert result.status_code==200,result.text
    snapshot=result.json();offset=0;records=[]
    while True:
        page=user.request('GET',f"/api/v1/sync/snapshot/{snapshot['snapshot_id']}?offset={offset}&limit=1")
        assert page.status_code==200,page.text
        body=page.json();records+=body['records'];offset=body['next_offset']
        if body['done']:break
    ack=user.request('POST','/api/v1/sync/ack',{'snapshot_id':snapshot['snapshot_id'],'cursor':snapshot['watermark']})
    assert ack.status_code==200,ack.text
    return snapshot,records


def test_snapshot_then_changes_include_updates_and_sharing(clients):
    _,alice,bob=clients
    first=put(alice,{'source':'test','source_id':'first','kind':'note','version':1,'payload':{'text':'version1'}})
    snapshot=alice.request('POST','/api/v1/sync/snapshot').json()
    put(alice,{'source':'test','source_id':'first','kind':'note','version':2,'payload':{'text':'version2'}})
    page=alice.request('GET',f"/api/v1/sync/snapshot/{snapshot['snapshot_id']}?limit=1").json()
    assert page['records'][0]['payload']['text']=='version1'
    assert alice.request('POST','/api/v1/sync/ack',{'snapshot_id':snapshot['snapshot_id'],'cursor':snapshot['watermark']}).status_code==200
    changes=alice.request('GET','/api/v1/sync/changes').json()
    assert changes['records'][0]['payload']['text']=='version2'
    initialize(bob)
    assert alice.request('PUT',f'/api/v1/data/{first}/grants/{bob.user_id}').status_code==200
    shared=bob.request('GET','/api/v1/sync/changes').json()
    assert shared['records'][0]['id']==first
    assert bob.request('POST','/api/v1/sync/ack',{'cursor':shared['next_cursor']}).status_code==200
    cached=bob.request('POST','/api/v1/sync/snapshot').json()['snapshot_id']
    assert alice.request('DELETE',f'/api/v1/data/{first}/grants/{bob.user_id}').status_code==200
    assert bob.request('GET','/api/v1/sync/snapshot/'+cached).status_code==404
    revoked=bob.request('GET','/api/v1/sync/changes').json()
    assert first in revoked['removed_ids'] and revoked['records']==[]


def test_upload_receipt_and_deleted_replay(clients):
    _,alice,bob=clients
    body={'batch_id':str(uuid.uuid4()),'records':[{'source':'batch','source_id':'item','kind':'note','version':1,'payload':{'text':'body'}}]}
    first=alice.request('POST','/api/v1/data/sync',body)
    assert first.status_code==200,first.text
    rid=first.json()['records'][0]['id']
    replay=alice.request('POST','/api/v1/data/sync',body).json()
    assert replay['replayed'] and replay['accepted_versions'][rid]==1
    changed={**body,'records':[{**body['records'][0],'payload':{'text':'changed'}}]}
    assert alice.request('POST','/api/v1/data/sync',changed).status_code==409
    assert alice.request('DELETE','/api/v1/data/'+rid).status_code==200
    replay=alice.request('POST','/api/v1/data/sync',body).json()
    assert replay['records']==[] and rid in replay['removed_ids']
    assert alice.request('GET','/api/v1/data/'+rid).status_code==404


def test_snapshot_owner_and_cursor_boundaries(clients):
    _,alice,bob=clients
    put(alice)
    snapshot=alice.request('POST','/api/v1/sync/snapshot').json()
    assert bob.request('GET','/api/v1/sync/snapshot/'+snapshot['snapshot_id']).status_code==404
    assert alice.request('GET','/api/v1/sync/snapshot/'+snapshot['snapshot_id']+'?offset=1').status_code==409
    assert alice.request('POST','/api/v1/sync/ack',{'cursor':snapshot['watermark'],'snapshot_id':snapshot['snapshot_id']}).status_code==409
    assert alice.request('GET','/api/v1/sync/changes').status_code==409
    initialize(alice)
    assert alice.request('POST','/api/v1/sync/ack',{'cursor':10**12}).status_code==409


def test_secret_upgrade_invalidates_shared_cache_and_rls(clients):
    app,alice,bob=clients
    item={'source':'test','source_id':'classified','kind':'note','version':1,'payload':{'text':'private'}}
    rid=put(alice,item)
    assert alice.request('PUT',f'/api/v1/data/{rid}/grants/{bob.user_id}').status_code==200
    snapshot,_=initialize(bob)
    item.update(version=2,sensitivity='SECRET')
    put(alice,item)
    assert bob.request('GET','/api/v1/data/'+rid).status_code==404
    assert bob.request('GET','/api/v1/sync/snapshot/'+snapshot['snapshot_id']).status_code==404
    assert rid in bob.request('GET','/api/v1/sync/changes').json()['removed_ids']
    from homeai.db import Record
    with app.db() as db:
        principal=db.get(Principal,bob.user_id);scope(db,bob.user_id,principal.household_id)
        assert db.get(Record,rid) is None


def test_snapshot_waits_for_uncommitted_record_transaction(clients):
    from concurrent.futures import ThreadPoolExecutor,TimeoutError
    from threading import Event
    from homeai.data import ingest
    from homeai.contracts import DataRecord
    from homeai.security import Actor
    app,alice,_=clients
    started=Event()
    def request_snapshot():
        started.set()
        return alice.request('POST','/api/v1/sync/snapshot')
    with ThreadPoolExecutor(max_workers=1) as executor:
        with app.db() as db:
            principal=db.get(Principal,alice.user_id)
            actor=Actor(alice.user_id,principal.household_id,alice.device_id,principal.role)
            record=ingest(db,actor,DataRecord(source='concurrent',source_id=str(uuid.uuid4()),kind='note',version=1,payload={'text':'commit boundary'}),app.vault)
            rid=record.id
            future=executor.submit(request_snapshot)
            assert started.wait(2)
            try:
                with pytest.raises(TimeoutError):future.result(timeout=0.2)
            finally:db.commit()
        response=future.result(timeout=10)
    assert response.status_code==200,response.text
    snapshot=response.json()
    page=alice.request('GET','/api/v1/sync/snapshot/'+snapshot['snapshot_id']).json()
    assert any(row['id']==rid for row in page['records'])


def test_source_metadata_is_owned_and_preserves_deletion(clients):
    _,alice,bob=clients
    rid=put(alice,{'source':'connector','source_id':'existing','kind':'note','version':4,'sensitivity':'SECRET','payload':{'text':'private'}})
    request={'source':'connector','source_id':'existing'}
    metadata=alice.request('POST','/api/v1/sync/source',request).json()
    assert metadata['version']==4 and metadata['sensitivity']=='SECRET'
    assert bob.request('POST','/api/v1/sync/source',request).json()['version']==0
    alice.request('DELETE','/api/v1/data/'+rid)
    assert alice.request('POST','/api/v1/sync/source',request).json()['deleted'] is True
