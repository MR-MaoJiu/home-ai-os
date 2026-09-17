"""真实模型自主多轮工具执行；不替换模型响应。"""
import os
import uuid
import pytest
from fastapi.testclient import TestClient
from homeai.api import create_app
from homeai.config import Settings
from homeai.contracts import ProviderManifest
from homeai.db import Provider
from homeai.runtime import run_task
from conftest import SignedClient

pytestmark = pytest.mark.skipif(os.environ.get('HOMEAI_MODEL_TEST') != '1', reason='需要真实 llama.cpp、OPA 和 PostgreSQL')


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(3))
async def test_real_agent_creates_two_reminders_then_summarizes(iteration):
    settings=Settings()
    settings.database_url=settings.database_url.rsplit('/',1)[0]+'/homeai_test'
    app=create_app(settings)
    user=SignedClient(TestClient(app),app.state.db,household=str(uuid.uuid4()))
    manifest=ProviderManifest(id='a.agent.local',version='8460',adapter='openai',endpoint='http://127.0.0.1:58080/v1',model='Qwen3-0.6B-Q8_0.gguf',allowed_hosts=['127.0.0.1'],capabilities={'model.generate@v1':'chat'})
    with app.state.db() as db:
        row=db.get(Provider,manifest.id)
        if row:row.manifest,row.enabled=manifest.model_dump_json(),True
        else:db.add(Provider(id=manifest.id,manifest=manifest.model_dump_json(),enabled=True))
        db.commit()
    try:
        response=user.request('POST','/api/v1/tasks',{'idempotency_key':str(uuid.uuid4()),'message':'请使用 create_reminder 工具分别创建两个提醒：买牛奶、带雨伞。必须调用两次工具，每次一个标题。收到工具成功结果后用一句话总结，不要重复创建。/no_think','max_steps':4,'max_output_tokens':512})
        assert response.status_code==202,response.text
        tid=response.json()['id']
        for _ in range(10):
            await run_task(app.state,tid,user.user_id)
            result=user.request('GET','/api/v1/tasks/'+tid).json()
            if result['status'] in {'SUCCEEDED','FAILED','NEEDS_RECONCILIATION','CANCELED'}:break
        assert result['status']=='SUCCEEDED',result
        steps=user.request('GET','/api/v1/tasks/'+tid+'/steps').json()
        assert len(steps)==2,steps
        titles=[]
        for step in steps:
            assert step['status']=='SUCCEEDED' and step['capability']=='reminder.create@v1'
            record=user.request('GET','/api/v1/data/'+step['result']['record_id']).json()
            titles.append(record['payload']['title'])
        assert set(titles)=={'买牛奶','带雨伞'},titles
        assert result['result']['choices'][0]['message']['content']
        # 已完成任务重新调度不能增加工具调用。
        await run_task(app.state,tid,user.user_id)
        assert len(user.request('GET','/api/v1/tasks/'+tid+'/steps').json())==2
    finally:
        with app.state.db() as db:
            db.get(Provider,manifest.id).enabled=False
            db.commit()
