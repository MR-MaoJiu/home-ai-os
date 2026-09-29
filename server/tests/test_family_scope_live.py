"""真实本地模型通过对话创建服务端自动化和聊天记忆。"""
import os,uuid
import pytest
from fastapi.testclient import TestClient
from homeai.api import create_app
from homeai.config import Settings
from homeai.contracts import ProviderManifest
from homeai.db import Provider,Automation,scope
from homeai.runtime import run_task
from conftest import SignedClient
from sqlalchemy import select
pytestmark=pytest.mark.skipif(os.getenv('HOMEAI_SCOPE_LIVE')!='1',reason='需要真实本地模型与 PostgreSQL')

@pytest.mark.asyncio
async def test_real_chat_drives_automation_and_memory():
    settings=Settings();settings.database_url=settings.database_url.rsplit('/',1)[0]+'/homeai_test'
    app=create_app(settings);client=TestClient(app)
    user=SignedClient(client,app.state.db,household=str(uuid.uuid4()))
    model=ProviderManifest(id='a.aaa.scope.'+uuid.uuid4().hex,version='1',adapter='openai',endpoint='http://127.0.0.1:58082/v1',model='Qwen3-4B-Q4_K_M.gguf',allowed_hosts=['127.0.0.1'],capabilities={'model.generate@v1':'chat'})
    with app.state.db() as db:db.add(Provider(id=model.id,manifest=model.model_dump_json(),enabled=True));db.commit()
    try:
        async def message(text):
            conversation=user.request('POST','/api/v1/conversations',{'client_id':str(uuid.uuid4())}).json()['id']
            task=user.request('POST',f'/api/v1/conversations/{conversation}/messages',{'client_key':str(uuid.uuid4()),'content':text}).json()['task_id']
            for _ in range(14):
                await run_task(app.state,task,user.user_id)
                result=user.request('GET','/api/v1/tasks/'+task).json()
                if result['status'] in ('SUCCEEDED','FAILED','CANCELED'):break
            assert result['status']=='SUCCEEDED',result.get('error')
        await message('请调用 create_automation 建立一个全家可见的定时任务，name=家庭喝水，cron=0 8 * * *，instruction=提醒全家喝水，visibility=family。无需现在执行提醒。/no_think')
        rules=user.request('GET','/api/v1/automations').json()
        assert len(rules)==1 and rules[0]['visibility']=='family'
        await message('我喜欢喝茶。请调用 remember_chat 为我创建候选记忆，content=用户喜欢喝茶；等待我到记忆页面确认即可，不必直接确认。/no_think')
        candidates=user.request('GET','/api/v1/memory/candidates').json()
        assert len(candidates)==1 and '茶' in candidates[0]['content']
        user.request('POST','/api/v1/memory/candidates/'+candidates[0]['id']+'/confirm')
        assert len(user.request('GET','/api/v1/memory/entries').json())==1
    finally:
        with app.state.db() as db:
            db.get(Provider,model.id).enabled=False;scope(db,user.user_id,user.request('GET','/api/v1/me').json()['household_id'])
            for rule in db.scalars(select(Automation).where(Automation.owner_id==user.user_id)):rule.enabled=False
            db.commit()
        user.request('DELETE','/api/v1/devices/'+user.device_id)
        client.close();app.state.db.kw['bind'].dispose()
