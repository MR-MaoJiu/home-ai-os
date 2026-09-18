import json,uuid
import pytest
from conftest import SignedClient
from homeai.db import Task,ConversationTurn,scope
from homeai.conversations import history,import_legacy
from homeai.security import Actor
from test_security_data import put,record


def new_chat(client):
    result=client.request('POST','/api/v1/conversations',{'client_id':str(uuid.uuid4())})
    assert result.status_code==200,result.text
    return result.json()['id']

def send(client,identifier,content,key=None):
    return client.request('POST','/api/v1/conversations/'+identifier+'/messages',{'client_key':key or str(uuid.uuid4()),'content':content})

def finish(system,client,task_id,text):
    with system[2]() as db:
        scope(db,client.user_id,'h1');task=db.get(Task,task_id);task.status='SUCCEEDED'
        task.result=system[0].state.vault.seal({'choices':[{'message':{'content':text}}]},client.user_id+':task-result:'+task.id);db.commit()


def test_persistent_chat_idempotence_scope_and_context(system,alice):
    identifier=new_chat(alice);key=str(uuid.uuid4())
    first=send(alice,identifier,'本次对话代号青竹42',key)
    assert first.status_code==202,first.text
    task=first.json()['task_id']
    assert send(alice,identifier,'本次对话代号青竹42',key).json()['id']==first.json()['id']
    assert send(alice,identifier,'不同文本',key).status_code==409
    assert send(alice,identifier,'并发下一条').status_code==409
    bob=SignedClient(system[1],system[2])
    assert bob.request('GET','/api/v1/conversations/'+identifier).status_code==404
    assert bob.request('GET','/api/v1/conversations').json()==[]
    finish(system,alice,task,'收到，代号是青竹42。')
    second=send(alice,identifier,'刚才的代号是什么？')
    assert second.status_code==202
    # 新客户端重新读取服务器，不依赖任何客户端内存。
    page=alice.request('GET','/api/v1/conversations/'+identifier).json()
    assert len(page['turns'])==2 and page['turns'][0]['assistant_text']=='收到，代号是青竹42。'
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,second.json()['task_id'])
        body=system[0].state.vault.open(task.request,alice.user_id+':task:'+task.id)
        actor=Actor(alice.user_id,'h1',alice.device_id,'infrastructure_owner')
        messages=history(system[0].state,db,actor,body)
        assert [m['role'] for m in messages]==['user','assistant'] and '青竹42' in messages[0]['content']
    assert send(bob,identifier,'越权').status_code==404
    assert alice.request('POST','/api/v1/conversations/'+identifier+'/messages',{'client_key':str(uuid.uuid4()),'content':'x','steps':[]}).status_code==422


def test_legacy_import_is_idempotent(system,alice):
    task=alice.request('POST','/api/v1/tasks',{'idempotency_key':str(uuid.uuid4()),'message':'旧单轮提问'}).json()['id']
    finish(system,alice,task,'旧回答')
    with system[2]() as db:
        actor=Actor(alice.user_id,'h1',alice.device_id,'infrastructure_owner')
        assert import_legacy(system[0].state,db,actor)==1
        assert import_legacy(system[0].state,db,actor)==0
    identifier=alice.request('GET','/api/v1/conversations').json()[0]['id']
    assert alice.request('GET','/api/v1/conversations/'+identifier).json()['turns'][0]['assistant_text']=='旧回答'


def test_history_does_not_revive_revoked_sources(system,alice):
    bob=SignedClient(system[1],system[2]);rid=put(bob,record(payload={'title':'共享资料'}))
    assert bob.request('PUT','/api/v1/data/'+rid+'/grants/'+alice.user_id).status_code==200
    identifier=new_chat(alice);first=send(alice,identifier,'读取共享资料').json()
    finish(system,alice,first['task_id'],'来自共享来源的旧回答')
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,first['task_id'])
        value=system[0].state.vault.open(task.request,alice.user_id+':task:'+task.id)
        value['_record_dependencies']={rid:1};task.request=system[0].state.vault.seal(value,alice.user_id+':task:'+task.id);db.commit()
    bob.request('DELETE','/api/v1/data/'+rid+'/grants/'+alice.user_id)
    page=alice.request('GET','/api/v1/conversations/'+identifier).json()
    assert '旧回答已隐藏' in page['turns'][0]['assistant_text']
    second=send(alice,identifier,'继续').json()
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,second['task_id'])
        payload=system[0].state.vault.open(task.request,alice.user_id+':task:'+task.id)
        assert history(system[0].state,db,Actor(alice.user_id,'h1',alice.device_id,'infrastructure_owner'),payload)==[]


def test_intent_is_orchestrated_by_server_and_history_is_available(system,alice):
    body={'idempotency_key':str(uuid.uuid4()),'title':'来自快捷入口的提醒'}
    first=alice.request('POST','/api/v1/input/reminder',body)
    assert first.status_code==202,first.text
    assert alice.request('POST','/api/v1/input/reminder',body).json()==first.json()
    page=alice.request('GET','/api/v1/conversations/'+first.json()['conversation_id']).json()
    assert page['turns'][0]['task_id']==first.json()['id']
    assert alice.request('POST','/api/v1/conversations/'+first.json()['conversation_id']+'/messages',{'client_key':str(uuid.uuid4()),'content':'x','timezone':'not/a/zone'}).status_code==422
