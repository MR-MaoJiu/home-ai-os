"""仅临时数据库：轻量快照、分页预算、撤权墓碑和游标兼容。"""
from sqlalchemy import select
from conftest import SignedClient
from homeai.crypto import canonical
from homeai.data import emit
from homeai.db import Outbox, Record, SyncCursor, SyncSnapshot, scope
from homeai.device_sync import MAX_PAGE_BYTES, MAX_INLINE_PAYLOAD_BYTES
from homeai.security import Actor
from test_security_data import put, record


def initialize(client):
    response = client.request('POST', '/api/v1/sync/snapshot')
    assert response.status_code == 200, response.text
    snapshot = response.json()
    records, offset = [], 0
    while True:
        response = client.request('GET', f"/api/v1/sync/snapshot/{snapshot['snapshot_id']}?offset={offset}&limit=200")
        assert response.status_code == 200, response.text
        assert len(response.content) < MAX_PAGE_BYTES
        page = response.json()
        records.extend(page['records'])
        if page['done']:
            break
        assert page['next_offset'] > offset
        offset = page['next_offset']
    ack = client.request('POST', '/api/v1/sync/ack', {'snapshot_id':snapshot['snapshot_id'], 'cursor':snapshot['watermark']})
    assert ack.status_code == 200, ack.text
    return snapshot, records


def batch(client, prefix, count, payload):
    response = client.request('POST', '/api/v1/data/sync', {'records':[
        record(source_id=prefix+str(i), payload=payload, kind='note') for i in range(count)]})
    assert response.status_code == 200, response.text
    return [item['id'] for item in response.json()['records']]


def test_snapshot_stores_only_ids_and_reads_current_projection(system, alice, monkeypatch):
    secret = 'A' * (1024 * 1024)
    rid = put(alice, record(payload={'name':'图片','content_base64':secret}, kind='photo.selected'))
    vault = system[0].state.vault
    original_open = vault.open
    opened_contexts = []
    def inspect_open(value, context):
        opened_contexts.append(context)
        return original_open(value, context)
    monkeypatch.setattr(vault, 'open', inspect_open)
    snapshot = alice.request('POST', '/api/v1/sync/snapshot').json()
    assert not any(':record:' in context for context in opened_contexts)
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        stored = db.get(SyncSnapshot, snapshot['snapshot_id'])
        assert original_open(stored.payload,alice.user_id+':sync-snapshot:'+stored.id) == [rid]
        assert len(stored.payload) < 1000
    put(alice, record(version=2,payload={'name':'图片新版','content_base64':secret},kind='photo.selected'))
    response = alice.request('GET','/api/v1/sync/snapshot/'+snapshot['snapshot_id'])
    item = response.json()['records'][0]
    assert item['version'] == 2 and item['payload']['name'] == '图片新版'
    assert item['payload_deferred'] and 'content_base64' not in item['payload']
    assert len(response.content) < 4096
    assert alice.request('GET','/api/v1/data/'+rid).json()['payload']['content_base64'] == secret


def test_large_payload_is_metadata_only_and_small_payload_is_unchanged(alice):
    small = {'title':'健康摘要','value':42,'unit':'分钟'}
    first = put(alice,record(source_id='small',payload=small))
    second = put(alice,record(source_id='large',payload={'title':'长文','text':'文'*20000}))
    third = put(alice,record(source_id='nested',payload={'title':'附件','attachments':[{'name':'音频','audio_base64':'AAAA'}]}))
    _, rows = initialize(alice)
    rows = {row['id']:row for row in rows}
    assert rows[first]['payload'] == small and rows[first]['payload_deferred'] is False
    assert rows[second]['payload'] == {'title':'长文'} and rows[second]['payload_deferred']
    assert rows[third]['payload_deferred'] and 'audio_base64' not in str(rows[third]['payload'])
    assert len(alice.request('GET','/api/v1/data/'+second).json()['payload']['text']) == 20000


def test_snapshot_byte_limit_advances_only_processed_ids(alice):
    ids = batch(alice, 'many', 90, {'text':'a'*7800})
    response = alice.request('POST','/api/v1/sync/snapshot')
    snapshot = response.json()
    offset, found, pages = 0, [], 0
    while True:
        response = alice.request('GET',f"/api/v1/sync/snapshot/{snapshot['snapshot_id']}?offset={offset}&limit=200")
        assert response.status_code == 200, response.text
        assert len(response.content) < MAX_PAGE_BYTES
        page = response.json(); pages += 1
        found.extend(item['id'] for item in page['records'])
        assert page['next_offset'] == len(found)
        if page['done']: break
        assert page['next_offset'] > offset
        offset = page['next_offset']
        assert alice.request('POST','/api/v1/sync/ack',{'snapshot_id':snapshot['snapshot_id'],'cursor':snapshot['watermark']}).status_code == 409
    assert set(found) == set(ids) and len(found) == len(ids) and pages >= 3
    assert alice.request('POST','/api/v1/sync/ack',{'snapshot_id':snapshot['snapshot_id'],'cursor':snapshot['watermark']}).status_code == 200


def test_change_byte_limit_does_not_skip_events_or_revocation(system, alice):
    bob = SignedClient(system[1], system[2])
    shared = put(bob, record(payload={'title':'共享资料'}))
    assert bob.request('PUT','/api/v1/data/'+shared+'/visibility',{'visibility':'family'}).status_code == 200
    snapshot, _ = initialize(alice)
    ids = batch(alice, 'changes', 90, {'text':'b'*7800})
    assert bob.request('PUT','/api/v1/data/'+shared+'/visibility',{'visibility':'personal'}).status_code == 200
    cursor, found, removed, pages = snapshot['watermark'], [], [], 0
    while True:
        response = alice.request('GET',f'/api/v1/sync/changes?after={cursor}&limit=200')
        assert response.status_code == 200,response.text
        assert len(response.content) < MAX_PAGE_BYTES
        page=response.json();pages+=1
        found.extend(item['id'] for item in page['records']);removed.extend(page['removed_ids'])
        assert page['next_cursor'] > cursor
        cursor=page['next_cursor']
        assert alice.request('POST','/api/v1/sync/ack',{'cursor':cursor}).status_code==200
        if not page['has_more']:break
    assert set(found) == set(ids) and shared in removed and pages>=3
    unchanged=alice.request('GET',f'/api/v1/sync/changes?after={cursor}').json()
    assert unchanged == {'records':[],'removed_ids':[],'next_cursor':cursor,'has_more':False}


def test_legacy_snapshot_rechecks_permission_and_never_uses_old_payload(system, alice):
    bob=SignedClient(system[1],system[2])
    rid=put(bob,record(payload={'title':'旧共享正文'}))
    bob.request('PUT','/api/v1/data/'+rid+'/visibility',{'visibility':'family'})
    snapshot=alice.request('POST','/api/v1/sync/snapshot').json()
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        row=db.get(SyncSnapshot,snapshot['snapshot_id'])
        row.payload=system[0].state.vault.seal([{'id':rid,'payload':{'title':'旧共享正文'}}],alice.user_id+':sync-snapshot:'+row.id)
        db.commit()
    put(bob,record(version=2,payload={'title':'已更新正文'}))
    page=alice.request('GET','/api/v1/sync/snapshot/'+snapshot['snapshot_id']).json()
    assert page['records'][0]['payload']['title']=='已更新正文'
    # 模拟读取时权限已经变化而旧快照仍存在，必须按当前权限返回墓碑。
    with system[2]() as db:
        scope(db,bob.user_id,'h1');db.get(Record,rid).visibility='personal';db.commit()
    page=alice.request('GET','/api/v1/sync/snapshot/'+snapshot['snapshot_id']).json()
    assert page['records']==[] and page['removed_ids']==[rid]


def test_unrelated_task_events_do_not_advance_data_cursor_and_old_ack_is_supported(system, alice):
    put(alice)
    initial,_=initialize(alice)
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        emit(db,Actor(alice.user_id,'h1',alice.device_id,'adult'),'task.updated','some-task')
        db.commit()
    result=alice.request('GET',f"/api/v1/sync/changes?after={initial['watermark']}").json()
    assert result['records']==[] and result['next_cursor']==initial['watermark']
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        old_high=db.scalar(select(Outbox.id).where(Outbox.owner_id==alice.user_id).order_by(Outbox.id.desc()))
        cursor=db.scalar(select(SyncCursor).where(SyncCursor.device_id==alice.device_id))
        cursor.acknowledged=cursor.offered=old_high
        db.commit()
    fresh,_=initialize(alice)
    assert fresh['watermark']==old_high
    assert alice.request('GET',f'/api/v1/sync/changes?after={old_high}').json()['next_cursor']==old_high
