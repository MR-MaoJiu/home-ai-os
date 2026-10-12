import json
import pytest
from homeai.db import Task,Provider,scope
from homeai.contracts import ProviderManifest
from homeai.security import Actor
from homeai.chat_delivery import deliver
from test_conversations import new_chat,send


@pytest.mark.asyncio
async def test_delivery_uses_original_question_and_only_its_tool_receipts(system,alice,monkeypatch):
    app=system[0].state;chat=new_chat(alice)
    first=send(alice,chat,'请创建一个公开测试提醒').json()
    send(alice,chat,'现在聊聊另一件事情')
    captured=[]
    async def reply(db,actor,manifest,capability,arguments,identifier,**kwargs):
        captured.append(arguments)
        return {'choices':[{'message':{'content':'提醒建好了。'},'finish_reason':'stop'}]}
    monkeypatch.setattr(app.registry,'invoke',reply)
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        manifest=ProviderManifest(id='local.delivery',version='1',adapter='openai',endpoint='http://127.0.0.1:58100/v1',allowed_hosts=['127.0.0.1'],model='test',capabilities={'model.generate@v1':'chat'})
        db.add(Provider(id=manifest.id,manifest=manifest.model_dump_json(),enabled=True));db.commit()
        task=db.get(Task,first['task_id']);body=app.vault.open(task.request,alice.user_id+':task:'+task.id);body['steps']=[{'capability':'reminder.create@v1','arguments':{'title':'公开测试'}}]
        response={'choices':[{'message':{'content':'创建成功，标题是公开测试。'}}]}
        result=await deliver(app,db,Actor(alice.user_id,'h1',alice.device_id,'adult'),task,body,response,[{'role':'tool','content':'{"status":"stored","title":"公开测试"}'}])
    request=json.loads(captured[0]['messages'][1]['content'])
    assert request['original_question']=='请创建一个公开测试提醒'
    assert '另一件事情' not in json.dumps(captured,ensure_ascii=False)
    assert result['choices'][0]['message']['content']=='提醒建好了。'
    assert body['_chat_delivery']['task_id']==first['task_id']
    assert 'tools' not in captured[0] and captured[0]['thinking']=={'type':'disabled'}


@pytest.mark.parametrize('status',['RECEIVED','EXECUTING','WAITING_CLIENT','AWAITING_APPROVAL','FAILED'])
def test_tool_step_receipt_is_not_a_completed_chat_answer(system,alice,status):
    chat=new_chat(alice);turn=alice.request('POST','/api/v1/conversations/'+chat+'/messages',{'client_key':'receipt-'+status,'schema_version':'2.0','content':'请处理公开测试任务'}).json()
    app=system[0].state
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,turn['task_id']);task.status=status
        task.result=app.vault.seal({'status':'stored','id':'one-step-only'},alice.user_id+':task-result:'+task.id);db.commit()
    result=alice.request('GET','/api/v1/conversations/'+chat).json()['turns'][0]
    assert '已保存到家庭服务器' not in result['assistant_text']
    assert result['assistant_text']=='' if status!='FAILED' else '没办成' in result['assistant_text']


@pytest.mark.asyncio
async def test_health_statistics_reach_chat_delivery_without_losing_nested_stage_evidence(system,alice,monkeypatch):
    app=system[0].state;chat=new_chat(alice)
    turn=send(alice,chat,'请分析本次提供的睡眠资料').json();captured=[]
    async def reply(db,actor,manifest,capability,arguments,identifier,**kwargs):
        captured.append(arguments)
        return {'choices':[{'message':{'content':'按已提供记录统计完了。'},'finish_reason':'stop'}]}
    monkeypatch.setattr(app.registry,'invoke',reply)
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        manifest=ProviderManifest(id='local.sleep-delivery',version='1',adapter='openai',endpoint='http://127.0.0.1:58100/v1',allowed_hosts=['127.0.0.1'],model='test',capabilities={'model.generate@v1':'chat'})
        db.add(Provider(id=manifest.id,manifest=manifest.model_dump_json(),enabled=True));db.commit()
        task=db.get(Task,turn['task_id']);body=app.vault.open(task.request,alice.user_id+':task:'+task.id)
        body['steps']=[{'capability':'client.request@v1','arguments':{'kind':'read_health'}}]
        receipt={'health':{'query_complete':None,'summary':{'metrics':{'sleep':{'asleep_seconds':7200,
            'stage_seconds':{'asleep_deep':1800},'daily':[{'date':'2026-01-01','asleep_seconds':7200}]}}}}}
        await deliver(app,db,Actor(alice.user_id,'h1',alice.device_id,'adult'),task,body,
            {'choices':[{'message':{'content':'已完成本次睡眠统计。'}}]},
            [{'role':'tool','content':json.dumps(receipt)}])
    evidence=json.loads(captured[0]['messages'][1]['content'])
    result=evidence['tool_receipts'][0]['result']['health']['summary']['metrics']['sleep']
    assert result['stage_seconds']['asleep_deep']==1800
    assert result['daily'][0]['asleep_seconds']==7200
