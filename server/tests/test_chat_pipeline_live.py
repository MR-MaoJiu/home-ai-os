"""真实本地模型、真实 Core 工具与策略的隔离多轮闭环，不碰运行库。"""
import os,asyncio,json
import httpx,pytest
from sqlalchemy import select
from homeai.db import Task,Provider,Invocation,Record,scope
from homeai.contracts import ProviderManifest
from homeai.config import Settings
from homeai.policy import Policy
from homeai.runtime import run_task
from test_conversations import new_chat,send

@pytest.mark.asyncio
async def test_real_model_memory_context_tool_and_original_question_delivery(system,alice,monkeypatch):
    if os.environ.get('HOMEAI_PIPELINE_LIVE')!='1':pytest.skip('需要显式启用真实本地模型验收')
    app=system[0].state;app.policy=Policy(Settings().opa_url)
    from homeai import agent
    original_audit=agent.audit
    def diagnostic(db,actor,action,resource,details=None):
        if action=='agent.failed':
            import sys,traceback
            kind,error,tb=sys.exc_info()
            print('isolated_failure',kind.__name__ if kind else None,[(os.path.basename(frame.filename),frame.lineno,frame.name) for frame in traceback.extract_tb(tb) if '/homeai/' in frame.filename])
            if getattr(error,'orig',None):print('isolated_database_error',str(error.orig)[:200])
            if isinstance(error,httpx.HTTPStatusError):print('public_upstream_error',error.response.status_code,error.response.text[:300])
        return original_audit(db,actor,action,resource,details)
    monkeypatch.setattr(agent,'audit',diagnostic)
    async with httpx.AsyncClient(trust_env=False) as client:
        response=await client.get('http://127.0.0.1:58100/v1/models');response.raise_for_status();model=response.json()['data'][0]['id']
    with system[2]() as db:
        manifest=ProviderManifest(id='local.pipeline',version='1',adapter='openai',endpoint='http://127.0.0.1:58100/v1',model=model,allowed_hosts=['127.0.0.1'],capabilities={'model.generate@v1':'chat'})
        db.add(Provider(id=manifest.id,manifest=manifest.model_dump_json(),enabled=True));db.commit()
    chat=new_chat(alice)
    assert send(alice,chat,'你叫小竹，我叫小林').json()['status']=='SUCCEEDED'
    async def complete(question):
        turn=send(alice,chat,question).json();identifier=turn['task_id']
        async with asyncio.timeout(100):
            for _ in range(14):
                await run_task(app,identifier,alice.user_id)
                with system[2]() as db:
                    scope(db,alice.user_id,'h1');task=db.get(Task,identifier)
                    status=task.status;error=task.error
                if status in {'SUCCEEDED','FAILED','WAITING_CLIENT','WAITING_PRIVACY','AWAITING_APPROVAL','NEEDS_RECONCILIATION'}:break
        result=next(t for t in alice.request('GET','/api/v1/conversations/'+chat).json()['turns'] if t['task_id']==identifier)
        print('public_case',question,'status',status,'answer',result['assistant_text'][:200],'error',error)
        if status!='SUCCEEDED':
            with system[2]() as db:
                scope(db,alice.user_id,'h1')
                print('public_tool_steps',[(row.capability,row.status) for row in db.scalars(select(Invocation).where(Invocation.task_id==identifier).order_by(Invocation.step))])
        assert status=='SUCCEEDED',error
        return identifier,result
    _,answer=await complete('你叫什么，我叫什么？')
    assert '小竹' in answer['assistant_text'] and '小林' in answer['assistant_text']
    _,answer=await complete('有点困，可是我还在上班啊')
    assert answer['interaction_kind']=='chat'
    assert not any(word in answer['assistant_text'] for word in ('IGNORED','候选','确认记忆'))
    identifier,answer=await complete('帮我创建一个提醒，标题是公开测试条目，不设时间')
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        invocations=list(db.scalars(select(Invocation).where(Invocation.task_id==identifier)))
        assert sum(row.capability=='reminder.create@v1' and row.status=='SUCCEEDED' for row in invocations)==1
        task=db.get(Task,identifier);body=app.vault.open(task.request,alice.user_id+':task:'+task.id)
        assert body['_chat_delivery']['task_id']==identifier
        print('public_delivery_status',body['_chat_delivery']['status'])
        assert body['_chat_delivery']['status']=='delivered'
    assert answer['assistant_text']
    _,answer=await complete('刚才那个提醒的标题是什么？')
    assert '公开测试条目' in answer['assistant_text']
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        assert len(list(db.scalars(select(Record).where(Record.kind=='reminder.item'))))==1

    _,mixed=await complete('记住我喜欢喝茶，帮我创建一个提醒，标题是公开复合测试，不设时间')
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        assert len(list(db.scalars(select(Record).where(Record.kind=='reminder.item'))))==2
        memories=[app.vault.open(row.payload,alice.user_id+':record:'+row.id)['content'] for row in db.scalars(select(Record).where(Record.kind=='memory.fact',Record.deleted.is_(False)))]
        assert any('喜欢喝茶' in item and '创建' not in item for item in memories)
