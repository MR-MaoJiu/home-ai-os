"""签名消息的引用身份、版本、来源和多轮上下文；仅使用独立测试数据库。"""
import json
import uuid
import pytest
from sqlalchemy import select
from conftest import SignedClient
from homeai.crypto import canonical, digest
from homeai.db import Task, ConversationTurn, Record, scope
from homeai.security import Actor
from homeai.conversations import history, Message
from test_conversations import new_chat, finish
from test_security_data import put, record


def send_reference(client, chat, identifier, version=1, content='分析这项数据', key=None, **extra):
    return client.request('POST',f'/api/v1/conversations/{chat}/messages',{
        'client_key':key or str(uuid.uuid4()),'content':content,
        'references':[{'record_id':identifier,'version':version}], **extra})


def payload(system, user, identifier):
    with system[2]() as db:
        scope(db,user.user_id,'h1'); task=db.get(Task,identifier)
        return system[0].state.vault.open(task.request,user.user_id+':task:'+identifier)


def test_reference_only_message_is_canonical_persistent_and_idempotent(system,alice):
    identifier=put(alice,record(payload={'title':'原始日程','content':'服务端保存的正文'}))
    chat=new_chat(alice); key=str(uuid.uuid4())
    response=send_reference(alice,chat,identifier,content='',key=key)
    assert response.status_code==202,response.text
    turn=response.json()
    assert turn['user_text']=='请查看这些数据。'
    assert turn['references']==[{'record_id':identifier,'version':1,'title':'原始日程','kind':'calendar.event'}]
    body=payload(system,alice,turn['task_id'])
    assert body['record_ids']==[identifier] and body['_record_dependencies']=={identifier:1}
    assert body['_memory_observation']['status'] not in {'SAVED','CANDIDATE'}
    assert send_reference(alice,chat,identifier,content='',key=key).json()['id']==turn['id']
    assert alice.request('GET',f'/api/v1/conversations/{chat}').json()['turns'][0]['references']==turn['references']
    assert send_reference(alice,chat,identifier,version=2,content='',key=key).status_code==409


@pytest.mark.parametrize('target',['private','other_household','deleted','secret','memory','stale'])
def test_reference_rejects_unauthorized_or_invalid_sources(system,alice,target):
    bob=SignedClient(system[1],system[2],household='h2' if target=='other_household' else 'h1')
    owner=bob if target in {'private','other_household'} else alice
    kind='memory.fact' if target=='memory' else 'calendar.event'
    identifier=put(owner,record(kind=kind))
    if target=='deleted':assert alice.request('DELETE',f'/api/v1/data/{identifier}').status_code==200
    if target=='secret':
        with system[2]() as db:
            db.get(Record,identifier).sensitivity='SECRET';db.commit()
    chat=new_chat(alice)
    response=send_reference(alice,chat,identifier,version=2 if target=='stale' else 1)
    assert response.status_code in {403,404,409},response.text
    assert alice.request('GET',f'/api/v1/conversations/{chat}').json()['turns']==[]


def test_shared_reference_is_revoked_with_task_and_chat_projection(system,alice):
    bob=SignedClient(system[1],system[2])
    identifier=put(bob)
    assert bob.request('PUT',f'/api/v1/data/{identifier}/visibility',{'visibility':'family'}).status_code==200
    chat=new_chat(alice); response=send_reference(alice,chat,identifier)
    assert response.status_code==202,response.text
    tid=response.json()['task_id'];finish(system,alice,tid,'有来源的回答')
    assert bob.request('PUT',f'/api/v1/data/{identifier}/visibility',{'visibility':'personal'}).status_code==200
    assert alice.request('GET',f'/api/v1/tasks/{tid}').json()['result'] is None
    view=alice.request('GET',f'/api/v1/conversations/{chat}').json()['turns'][0]
    assert view['references']==[] and '撤权' in view['assistant_text']
    assert send_reference(alice,chat,identifier).status_code==404


def test_followup_retains_reference_and_source_versions(system,alice):
    identifier=put(alice,record(payload={'title':'季度数据','content':'公开代号 Q91'}))
    chat=new_chat(alice); first=send_reference(alice,chat,identifier).json()
    finish(system,alice,first['task_id'],'第一轮引用答复')
    next_turn=alice.request('POST',f'/api/v1/conversations/{chat}/messages',{'client_key':str(uuid.uuid4()),'content':'这份数据里的代号是什么？'}).json()
    with system[2]() as db:
        actor=Actor(alice.user_id,'h1',alice.device_id,'infrastructure_owner')
        body=payload(system,alice,next_turn['task_id'])
        messages=history(system[0].state,db,actor,body)
        assert identifier in body['_historical_record_ids']
        assert body['_record_dependencies'][identifier]==1
        assert '季度数据' in json.dumps(messages,ensure_ascii=False)


def test_reference_and_attachment_have_one_canonical_dependency(system,alice):
    from test_media import upload
    asset=upload(alice,b'file content','note.txt','file').json()['asset']
    chat=new_chat(alice)
    response=send_reference(alice,chat,asset['record_id'],parts=[{'type':'file','record_id':asset['record_id'],'version':1}])
    assert response.status_code==202,response.text
    assert payload(system,alice,response.json()['task_id'])['record_ids']==[asset['record_id']]


@pytest.mark.parametrize('reference',[{'record_id':'x','version':True},{'record_id':'x','version':0},
    {'record_id':'x','version':1,'title':'伪造'}, {'record_id':'x','version':1,'owner_id':'someone'}])
def test_reference_wire_contract_does_not_accept_trusted_fields(alice,reference):
    chat=new_chat(alice)
    response=alice.request('POST',f'/api/v1/conversations/{chat}/messages',{
        'client_key':str(uuid.uuid4()),'content':'x','references':[reference]})
    assert response.status_code==422


def test_previous_version_pending_send_hash_is_still_replayable(system,alice):
    chat=new_chat(alice);key=str(uuid.uuid4())
    message=Message(client_key=key,content='旧客户端消息')
    response=alice.request('POST',f'/api/v1/conversations/{chat}/messages',{'client_key':key,'content':message.content})
    assert response.status_code==202
    previous=message.model_dump(mode='json');previous.pop('references')
    with system[2]() as db:
        turn=db.scalar(select(ConversationTurn).where(ConversationTurn.client_key==key))
        turn.request_hash=digest(canonical(previous));db.commit()
    replay=alice.request('POST',f'/api/v1/conversations/{chat}/messages',{'client_key':key,'content':message.content})
    assert replay.status_code==202 and replay.json()['id']==response.json()['id']
