"""记忆与业务数据的双向通道隔离，不依赖客户端隐藏或模型提示词。"""
import uuid
import pytest
from sqlalchemy import select
from conftest import SignedClient
from homeai.db import Record, Task, scope
from homeai.crypto import digest
from homeai.security import Actor
from homeai.task_events import snapshot
from test_security_data import put,record
from test_conversations import new_chat,send,finish


def remember(alice):
    chat=new_chat(alice)
    turn=send(alice,chat,'我喜欢喝茶').json()
    rows=alice.request('GET','/api/v1/memory/entries').json()
    assert len(rows)==1
    return rows[0]['id'],turn


def test_chat_memory_never_enters_data_listing_or_data_sync(system,alice):
    rid,turn=remember(alice)
    assert alice.request('GET','/api/v1/data').json()['records']==[]
    assert alice.request('GET','/api/v1/data/'+rid).status_code==404
    page=alice.request('POST','/api/v1/sync/snapshot').json()
    assert page['count']==0
    data=alice.request('GET','/api/v1/sync/snapshot/'+page['snapshot_id']).json()
    assert data['records']==[]
    assert alice.request('POST','/api/v1/sync/ack',{'snapshot_id':page['snapshot_id'],'cursor':page['watermark']}).status_code==200
    assert alice.request('GET','/api/v1/sync/changes').json()['records']==[]
    actor=Actor(alice.user_id,'h1',alice.device_id,'infrastructure_owner')
    status=snapshot(system[0].state,actor,digest(alice.token.encode()),None)
    assert status['memory_revision']>0 and status['data_revision']==0
    finish(system,alice,turn['task_id'],'已记住')


@pytest.mark.parametrize('item',[
    record(kind='memory.fact'),record(kind='memory.preference'),record(kind='client_input.text'),
    {**record(),'source':'conversation_memory'}, {**record(),'source':'memory_candidate'},
])
def test_data_upload_cannot_forge_memory_or_internal_input(system,alice,item):
    response=alice.request('POST','/api/v1/data/sync',{'records':[record(source_id='ordinary'),item]})
    assert response.status_code==403,response.text
    assert alice.request('GET','/api/v1/data').json()['records']==[]
    assert alice.request('GET','/api/v1/memory/entries').json()==[]


def test_memory_and_data_management_endpoints_are_not_interchangeable(system,alice):
    memory,turn=remember(alice)
    data=put(alice,record(source_id='file-record',kind='document.text',payload={'content':'个人资料'}))
    assert alice.request('DELETE','/api/v1/data/'+memory).status_code==403
    assert alice.request('PATCH','/api/v1/memory/entries/'+data,{'content':'不能改变分类','version':1}).status_code==404
    assert alice.request('DELETE','/api/v1/memory/entries/'+data).status_code==404
    assert alice.request('GET','/api/v1/data/'+data).status_code==200
    assert alice.request('DELETE','/api/v1/memory/entries/'+memory).status_code==200
    assert alice.request('GET','/api/v1/memory/entries').json()==[]
    assert alice.request('GET','/api/v1/data/'+data).status_code==200
    assert alice.request('GET','/api/v1/data').json()['records'][0]['id']==data


def test_memory_upload_source_cannot_be_looked_up_through_data_sync(alice):
    assert alice.request('POST','/api/v1/sync/source',{'source':'conversation_memory','source_id':'x'}).status_code==403


def test_existing_legacy_memory_remains_personal_and_is_not_data(system,alice):
    rid=put(alice,record(kind='memory.fact',source_id='legacy-memory',payload={'content':'旧版个人记忆'}))
    bob=SignedClient(system[1],system[2])
    assert alice.request('GET','/api/v1/data').json()['records']==[]
    assert [item['id'] for item in alice.request('GET','/api/v1/memory/entries').json()]==[rid]
    assert bob.request('GET','/api/v1/memory/entries').json()==[]
    assert bob.request('GET','/api/v1/memory/entries/'+rid).status_code==404


def test_referenced_document_cannot_be_converted_to_chat_memory(system,alice):
    rid=put(alice,record(kind='document.text',payload={'content':'文档事实'}))
    chat=new_chat(alice)
    response=alice.request('POST',f'/api/v1/conversations/{chat}/messages',{
        'client_key':str(uuid.uuid4()),'content':'请记住此资料','references':[{'record_id':rid,'version':1}]})
    assert response.status_code==202
    turn=response.json()
    candidate=alice.request('POST','/api/v1/memory/from-conversation',{
        'conversation_id':chat,'turn_id':turn['id'],'content':'文档事实'})
    assert candidate.status_code==403
    assert alice.request('GET','/api/v1/memory/entries').json()==[]
    assert alice.request('GET','/api/v1/data/'+rid).status_code==200
