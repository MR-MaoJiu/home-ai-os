"""旧家居计划缺配置时保留真实完成步骤，不伪造状态或执行依赖操作。"""
import json
import uuid
import pytest
from sqlalchemy import select, func
from homeai.db import Task, Record, Invocation
from homeai.runtime import run_task


@pytest.mark.asyncio
async def test_old_missing_home_plan_does_not_replay_success_or_run_dependent_effect(system, alice):
    app = system[0].state
    result = alice.request('POST','/api/v1/tasks',{'idempotency_key':str(uuid.uuid4()),'steps':[
        {'capability':'reminder.create@v1','arguments':{'title':'独立已完成事项'}},
        {'capability':'home.states@v1','arguments':{}},
        {'capability':'reminder.create@v1','arguments':{'title':'未取得家居状态前不应执行'}}]})
    assert result.status_code == 202, result.text
    identifier = result.json()['id']
    await run_task(app, identifier, alice.user_id)
    with system[2]() as db:
        task = db.get(Task, identifier)
        body = app.vault.open(task.request, alice.user_id+':task:'+identifier)
        first = db.scalar(select(Invocation).where(Invocation.task_id==identifier, Invocation.step==0))
        original = (first.id, first.result)
        body['_agent'] = True
        body['_agent_batches'] = [{'steps':[1,2],'message':{'role':'assistant','content':None,'tool_calls':[
            {'id':'old-home','type':'function','function':{'name':'read_home_states','arguments':'{}'}},
            {'id':'dependent','type':'function','function':{'name':'create_reminder','arguments':json.dumps(body['steps'][2]['arguments'])}}]}}]
        task.request = app.vault.seal(body, alice.user_id+':task:'+identifier); db.commit()
    await run_task(app, identifier, alice.user_id)
    with system[2]() as db:
        task = db.get(Task, identifier)
        rows = list(db.scalars(select(Invocation).where(Invocation.task_id==identifier).order_by(Invocation.step)))
        assert [row.status for row in rows] == ['SUCCEEDED','UNAVAILABLE','SKIPPED']
        assert (rows[0].id, rows[0].result) == original
        unavailable = app.vault.open(rows[1].result, alice.user_id+':invocation-result:'+rows[1].id)
        assert unavailable['not_dispatched'] is True and '未读取' in unavailable['message']
        skipped = app.vault.open(rows[2].result, alice.user_id+':invocation-result:'+rows[2].id)
        assert skipped['status']=='not_executed' and skipped['blocked_by_step']==1
        assert task.status=='RECEIVED' and task.error is None
        assert db.scalar(select(func.count()).select_from(Record).where(Record.kind=='reminder.item',Record.owner_id==alice.user_id))==1


@pytest.mark.asyncio
async def test_direct_missing_home_request_has_clear_human_reason(system,alice):
    response=alice.request('POST','/api/v1/tasks',{'idempotency_key':str(uuid.uuid4()),'capability':'home.states@v1','arguments':{}})
    await run_task(system[0].state,response.json()['id'],alice.user_id)
    result=alice.request('GET','/api/v1/tasks/'+response.json()['id']).json()
    assert result['status']=='FAILED'
    assert '智能家居' in result['error'] and 'Home Assistant' in result['error']
    assert 'home.states@v1' not in result['error']


@pytest.mark.asyncio
async def test_all_tools_unavailable_cannot_be_reported_as_success(system,alice,monkeypatch):
    from test_conversations import new_chat,send
    from homeai.db import Provider,scope
    from homeai.contracts import ProviderManifest
    app=system[0].state
    turn=send(alice,new_chat(alice),'读取家居状态').json();identifier=turn['task_id']
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        manifest=ProviderManifest(id='local.unavailable',version='1',adapter='openai',endpoint='http://127.0.0.1:58100/v1',allowed_hosts=['127.0.0.1'],model='test',capabilities={'model.generate@v1':'chat'})
        db.add(Provider(id=manifest.id,manifest=manifest.model_dump_json(),enabled=True))
        task=db.get(Task,identifier);body=app.vault.open(task.request,alice.user_id+':task:'+identifier)
        body['steps']=[{'capability':'home.states@v1','arguments':{}}]
        body['_agent_batches']=[{'steps':[0],'message':{'role':'assistant','content':'','tool_calls':[{'id':'unavailable-call','type':'function','function':{'name':'read_home_states','arguments':'{}'}}]}}]
        task.request=app.vault.seal(body,alice.user_id+':task:'+identifier);db.commit()
    async def mistaken_report(*args,**kwargs):return {'choices':[{'message':{'content':'已经完成全部操作。'},'finish_reason':'stop'}]}
    monkeypatch.setattr(app.registry,'invoke',mistaken_report)
    await run_task(app,identifier,alice.user_id)
    await run_task(app,identifier,alice.user_id)
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,identifier)
        assert task.status=='FAILED'
        result=app.vault.open(task.result,alice.user_id+':task-result:'+identifier)
        assert '没办成' in result['choices'][0]['message']['content']
        assert db.scalar(select(func.count()).select_from(Record).where(Record.kind=='reminder.item'))==0
