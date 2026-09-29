"""新版个人/家庭边界及对话驱动的服务端自动化。"""
import uuid
import pytest
from sqlalchemy import select
from conftest import SignedClient
from test_security_data import put,record
from homeai.db import Task,Automation,AutomationDelivery,scope,now
from homeai.runtime import run_task
from homeai.worker import cycle


def test_family_data_and_chat_memory_are_distinct(system,alice):
    bob=SignedClient(system[1],system[2]);outsider=SignedClient(system[1],system[2],household='other')
    rid=put(alice);path='/api/v1/data/'+rid
    assert bob.request('GET',path).status_code==404
    assert alice.request('PUT',path+'/visibility',{'visibility':'family'}).status_code==200
    assert bob.request('GET',path).json()['visibility']=='family'
    assert outsider.request('GET',path).status_code==404
    assert bob.request('PUT',path+'/visibility',{'visibility':'personal'}).status_code==404
    assert alice.request('PUT',path+'/visibility',{'visibility':'personal'}).status_code==200
    assert bob.request('GET',path).status_code==404
    assert alice.request('POST','/api/v1/memory/candidates',{'content':'原始文件','source_ids':[rid]}).status_code==410
    conversation=alice.request('POST','/api/v1/conversations',{'client_id':str(uuid.uuid4())}).json()['id']
    turn=alice.request('POST',f'/api/v1/conversations/{conversation}/messages',{'content':'我喜欢茶，请记住','client_key':str(uuid.uuid4())}).json()['id']
    candidate=alice.request('POST','/api/v1/memory/from-conversation',{'conversation_id':conversation,'turn_id':turn,'content':'用户喜欢茶'}).json()['id']
    assert bob.request('GET','/api/v1/memory/candidates').json()==[]
    fact=alice.request('POST','/api/v1/memory/candidates/'+candidate+'/confirm').json()['record_id']
    assert len(alice.request('GET','/api/v1/memory/entries').json())==1
    assert bob.request('GET','/api/v1/memory/entries').json()==[]
    assert alice.request('PUT','/api/v1/data/'+fact+'/visibility',{'visibility':'family'}).status_code==403


@pytest.mark.asyncio
async def test_server_schedules_and_exposes_only_family_status(system,alice):
    bob=SignedClient(system[1],system[2])
    task=alice.request('POST','/api/v1/tasks',{'idempotency_key':str(uuid.uuid4()),'capability':'automation.create@v1','arguments':{'name':'家庭例行提醒','cron':'0 8 * * *','instruction':'创建喝水提醒','visibility':'family'}}).json()['id']
    await run_task(system[0].state,task,alice.user_id)
    result=alice.request('GET','/api/v1/tasks/'+task).json()
    assert result['status']=='SUCCEEDED',result
    rule_id=result['result']['automation_id']
    rules=bob.request('GET','/api/v1/automations').json()
    assert len(rules)==1 and rules[0]['visibility']=='family' and rules[0]['instruction'] is None
    with system[2]() as db:
        scope(db,alice.user_id,'h1');rule=db.get(Automation,rule_id);rule.next_run=now()-1;db.commit()
    await cycle(system[0].state)
    runs=bob.request('GET','/api/v1/automations/'+rule_id+'/runs').json()
    assert len(runs)==1 and 'task_id' not in runs[0]
    with system[2]() as db:
        scope(db,alice.user_id,'h1');rule=db.get(Automation,rule_id)
        delivery=db.scalar(select(AutomationDelivery).where(AutomationDelivery.automation_id==rule_id))
        queued=db.get(Task,delivery.task_id);body=system[0].state.vault.open(queued.request,alice.user_id+':task:'+queued.id)
        assert body['_automation_scope']=='family' and body['_agent']
        assert body['device_id']!=alice.device_id
    assert bob.request('DELETE','/api/v1/automations/'+rule_id).status_code==404


def test_family_secret_promotion_revokes_cached_access(system,alice):
    bob=SignedClient(system[1],system[2]);rid=put(alice)
    assert alice.request('PUT','/api/v1/data/'+rid+'/visibility',{'visibility':'family'}).status_code==200
    snapshot=bob.request('POST','/api/v1/sync/snapshot').json()
    item=record(version=2);item['sensitivity']='SECRET';put(alice,item)
    assert bob.request('GET','/api/v1/data/'+rid).status_code==404
    assert bob.request('GET','/api/v1/sync/snapshot/'+snapshot['snapshot_id']).status_code==404


def test_parsed_document_inherits_and_revokes_family_scope(system,alice):
    from homeai.documents import persist
    from homeai.security import Actor
    bob=SignedClient(system[1],system[2])
    source=put(alice,record(kind='document.file',payload={'name':'家庭资料'}))
    alice.request('PUT','/api/v1/data/'+source+'/visibility',{'visibility':'family'})
    with system[2]() as db:
        parsed=persist(db,Actor(alice.user_id,'h1',alice.device_id,'adult'),source,1,{'markdown':'解析结果'},system[0].state)
        db.commit()
    path='/api/v1/data/'+parsed['record_id']
    assert bob.request('GET',path).status_code==200
    alice.request('PUT','/api/v1/data/'+source+'/visibility',{'visibility':'personal'})
    assert bob.request('GET',path).status_code==404
