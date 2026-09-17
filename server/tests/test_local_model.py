import os
import uuid
import pytest
from fastapi.testclient import TestClient
from homeai.api import create_app
from homeai.config import Settings
from homeai.runtime import run_task
from conftest import SignedClient

pytestmark=pytest.mark.skipif(os.environ.get('HOMEAI_MODEL_TEST')!='1',reason='需要运行本地 llama.cpp 模型')

@pytest.mark.asyncio
async def test_real_local_model():
    settings = Settings()
    settings.database_url = settings.database_url.rsplit("/", 1)[0] + "/homeai_test"
    app=create_app(settings)
    client=TestClient(app)
    actor=SignedClient(client,app.state.db,household=str(uuid.uuid4()),role='infrastructure_owner')
    manifest={'id':'test.llama','version':'1.0.0','adapter':'openai','endpoint':'http://127.0.0.1:58080/v1','model':'Qwen3-0.6B-Q8_0.gguf','capabilities':{'model.generate@v1':'chat'},'allowed_hosts':['127.0.0.1']}
    assert actor.request('PUT','/api/v1/providers/test.llama',manifest).status_code==200
    assert actor.request('POST','/api/v1/providers/test.llama/enable').status_code==200
    response=actor.request('POST','/api/v1/tasks',{'capability':'model.generate@v1','message':'请用一句中文介绍你能作为家庭助手做什么。/no_think','max_output_tokens':96,'idempotency_key':str(uuid.uuid4())})
    assert response.status_code==202,response.text
    task_id=response.json()['id']
    await run_task(app.state,task_id,actor.user_id)
    result=actor.request('GET','/api/v1/tasks/'+task_id).json()
    assert result['status']=='SUCCEEDED',result
    assert len(result['result']['choices'][0]['message']['content'])>0
    assert result['result']['usage']['completion_tokens']>0
