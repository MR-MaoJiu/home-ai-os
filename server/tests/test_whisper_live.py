"""真实 whisper.cpp 转写；英文人声和中文合成音频仅作测试输入。"""
import base64,os,uuid
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from homeai.api import create_app
from homeai.config import Settings
from homeai.contracts import ProviderManifest
from homeai.db import Principal,Provider,Secret,scope,uid
from homeai.runtime import run_task
from conftest import SignedClient

pytestmark=pytest.mark.skipif(os.environ.get('HOMEAI_WHISPER_TEST')!='1',reason='需要真实 whisper.cpp、模型、语音桥、PostgreSQL、OPA 和音频文件')


@pytest.mark.asyncio
@pytest.mark.parametrize('filename,language,expected',[
    ('state/whisper.cpp/samples/jfk.wav','en',['ask not','country']),
    ('state/voice-fixtures/chinese.wav','zh',['明天','三点','备份']),
    ('state/voice-fixtures/chinese.wav','auto',['明天','三点','备份']),
    ('state/whisper.cpp/samples/jfk.wav','auto',['ask not','country']),
],ids=['english-human-sample','chinese-synthesized-fixture','chinese-auto','english-auto'])
async def test_real_transcription(filename,language,expected):
    settings=Settings();settings.database_url=settings.database_url.rsplit('/',1)[0]+'/homeai_test'
    app=create_app(settings);user=SignedClient(TestClient(app),app.state.db,household=str(uuid.uuid4()))
    provider_id='a.whisper.live';secret_id=uid();token=Path('state/provider-secrets/whisper.token').read_text().strip()
    with app.state.db() as db:
        principal=db.get(Principal,user.user_id);scope(db,user.user_id,principal.household_id)
        db.add(Secret(id=secret_id,owner_id=user.user_id,household_id=principal.household_id,provider_id=provider_id,value=app.state.vault.seal(token,user.user_id+':secret:'+secret_id)))
        manifest=ProviderManifest(id=provider_id,version='1.9.3',adapter='http',endpoint='http://127.0.0.1:8105',allowed_hosts=['127.0.0.1'],secret_id=secret_id,timeout_seconds=180,capabilities={'speech.transcribe@v1':'/invoke/transcribe'})
        row=db.get(Provider,provider_id)
        if row:row.manifest,row.enabled=manifest.model_dump_json(),True
        else:db.add(Provider(id=provider_id,manifest=manifest.model_dump_json(),enabled=True))
        db.commit()
    try:
        response=user.request('POST','/api/v1/tasks',{'idempotency_key':str(uuid.uuid4()),'capability':'speech.transcribe@v1','step_timeout_seconds':180,'arguments':{'content_base64':base64.b64encode(Path(filename).read_bytes()).decode(),'language':language}})
        assert response.status_code==202,response.text
        tid=response.json()['id'];await run_task(app.state,tid,user.user_id)
        task=user.request('GET','/api/v1/tasks/'+tid).json()
        assert task['status']=='SUCCEEDED',task
        assert all(word in task['result']['text'].lower() for word in expected),task['result']
        assert task['result']['provider']=='whisper.cpp'
        assert task['result']['duration_seconds']>0
    finally:
        with app.state.db() as db:db.get(Provider,provider_id).enabled=False;db.commit()
