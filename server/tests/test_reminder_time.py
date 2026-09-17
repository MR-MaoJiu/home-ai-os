"""确定性提醒时间契约与真实任务写入。"""
import os
import pytest
from fastapi import HTTPException
from homeai.reminders import normalize
from homeai.runtime import run_task
from test_workflows import workflow, create


def test_due_time_normalization_and_rejection():
    assert normalize({'title':'检查资料','due_at':'2026-10-01T08:30:00+08:00','notify_at_due':True})['due_at']=='2026-10-01T00:30:00Z'
    for args in [
        {'title':'检查','due_at':'2026-10-01T08:30:00'},
        {'title':'检查','notify_at_due':True},
        {'title':'检查','notify_at_due':'yes'},
        {'title':'检查','completed':'true'},
    ]:
        with pytest.raises(HTTPException):normalize(args)


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv('HOMEAI_INTEGRATION')!='1',reason='需要真实 PostgreSQL/OPA')
async def test_real_reminder_stores_absolute_time(workflow):
    app,user=workflow
    tid=create(user,[{'capability':'reminder.create@v1','arguments':{'title':'带时间的真实提醒','due_at':'2026-10-01T08:30:00+08:00','notify_at_due':True}}])
    await run_task(app,tid,user.user_id)
    task=user.request('GET','/api/v1/tasks/'+tid).json()
    assert task['status']=='SUCCEEDED',task
    record=user.request('GET','/api/v1/data/'+task['result']['record_id']).json()
    assert record['payload']['due_at']=='2026-10-01T00:30:00Z'
    assert record['payload']['notify_at_due'] is True
    assert record['cloud_policy']=='LOCAL_ONLY'


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv('HOMEAI_INTEGRATION')!='1' or os.getenv('HOMEAI_MODEL_TEST')!='1',reason='需要真实本地模型')
async def test_agent_uses_explicit_scheduled_reminder_tool(workflow):
    import uuid
    from sqlalchemy import select
    from homeai.db import Provider, Invocation, Principal, scope
    from homeai.contracts import ProviderManifest
    app,user=workflow
    manifest=ProviderManifest(id='a.agent.schedule.'+uuid.uuid4().hex,version='8460',adapter='openai',
        endpoint=os.getenv('HOMEAI_AGENT_TEST_URL','http://127.0.0.1:58082/v1'),model=os.getenv('HOMEAI_AGENT_TEST_MODEL','Qwen3-4B-Q4_K_M.gguf'),
        allowed_hosts=['127.0.0.1'],capabilities={'model.generate@v1':'chat'})
    with app.db() as db:
        db.add(Provider(id=manifest.id,manifest=manifest.model_dump_json(),enabled=True));db.commit()
    try:
        response=user.request('POST','/api/v1/tasks',{'idempotency_key':str(uuid.uuid4()),'message':'请使用 schedule_reminder 创建一条提醒，标题为检查资料，到期时间为 2026-10-01T08:30:00+08:00，notify_at_due 为 true。成功后简单总结，不要重复调用。/no_think','timezone':'Asia/Shanghai','max_steps':3,'max_output_tokens':512})
        assert response.status_code==202,response.text
        tid=response.json()['id']
        for _ in range(8):
            await run_task(app,tid,user.user_id)
            task=user.request('GET','/api/v1/tasks/'+tid).json()
            if task['status'] in {'SUCCEEDED','FAILED'}:break
        assert task['status']=='SUCCEEDED',task
        with app.db() as db:
            principal=db.get(Principal,user.user_id);scope(db,user.user_id,principal.household_id)
            calls=list(db.scalars(select(Invocation).where(Invocation.task_id==tid,Invocation.capability=='reminder.create@v1')))
            assert len(calls)==1
            receipt=app.vault.open(calls[0].result,user.user_id+':invocation-result:'+calls[0].id)
        record=user.request('GET','/api/v1/data/'+receipt['record_id']).json()
        assert record['payload']['due_at']=='2026-10-01T00:30:00Z'
        assert record['payload']['notify_at_due'] is True
    finally:
        with app.db() as db:
            db.get(Provider,manifest.id).enabled=False;db.commit()
