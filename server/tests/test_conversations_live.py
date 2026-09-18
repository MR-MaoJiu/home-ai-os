"""真实 PostgreSQL、模型和搜索引擎验证多轮会话，不提供模型替身。"""
import os,uuid
import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from homeai.api import create_app
from homeai.config import Settings
from homeai.contracts import ProviderManifest
from homeai.db import Provider
from homeai.runtime import run_task
from conftest import SignedClient

pytestmark=pytest.mark.skipif(os.getenv('HOMEAI_CONVERSATION_LIVE')!='1',reason='需要真实本地模型与 SearXNG')

@pytest.mark.asyncio
async def test_real_multi_turn_and_automatic_search():
    settings=Settings();settings.database_url=settings.database_url.rsplit('/',1)[0]+'/homeai_test'
    app=create_app(settings);client=TestClient(app);user=SignedClient(client,app.state.db,household=str(uuid.uuid4()))
    model=ProviderManifest(id='a.aaa.conversation.'+uuid.uuid4().hex,version='1.0.0',adapter='openai',endpoint='http://127.0.0.1:58082/v1',model='Qwen3-4B-Q4_K_M.gguf',allowed_hosts=['127.0.0.1'],capabilities={'model.generate@v1':'chat'})
    search=ProviderManifest.model_validate_json(Path('providers/manifests/searxng.json').read_text());search.id='a.aaa.search.'+uuid.uuid4().hex
    with app.state.db() as db:
        db.add_all([Provider(id=m.id,manifest=m.model_dump_json(),enabled=True) for m in (model,search)]);db.commit()
    try:
        identifier=user.request('POST','/api/v1/conversations',{'client_id':str(uuid.uuid4())}).json()['id']
        async def send(message):
            response=user.request('POST','/api/v1/conversations/'+identifier+'/messages',{'client_key':str(uuid.uuid4()),'content':message})
            assert response.status_code==202,response.text
            task=response.json()['task_id']
            for _ in range(24):
                await run_task(app.state,task,user.user_id)
                result=user.request('GET','/api/v1/tasks/'+task).json()
                assert result['status']!='AWAITING_APPROVAL','公开搜索不应要求审批'
                if result['status'] in {'SUCCEEDED','FAILED','CANCELED'}:break
            assert result['status']=='SUCCEEDED',result.get('error')
            return user.request('GET','/api/v1/conversations/'+identifier).json()['turns'][-1]
        await send('本次对话的代号是青竹42。只回复收到，不需要创建提醒。/no_think')
        second=await send('刚才约定的代号是什么？只回复代号。/no_think')
        assert '青竹42' in second['assistant_text'].replace(' ','')
        third=await send('请调用 search_web 联网检索 Python official documentation，整合两点信息并附真实来源。/no_think')
        assert third['web_sources'] and third['assistant_text']
        page=user.request('GET','/api/v1/conversations/'+identifier).json()
        assert len(page['turns'])==3
        reopened=TestClient(create_app(settings))
        path='/api/v1/conversations/'+identifier
        restored=reopened.get(path,headers=user.headers('GET',path,b''))
        assert restored.status_code==200 and len(restored.json()['turns'])==3
        reopened.close()
        print('REAL_CHAT multi_turn_context_and_auto_search_verified',flush=True)
    finally:
        with app.state.db() as db:
            for m in (model,search):db.get(Provider,m.id).enabled=False
            db.commit()
        user.request('DELETE','/api/v1/devices/'+user.device_id)
        app.state.db.kw['bind'].dispose()
