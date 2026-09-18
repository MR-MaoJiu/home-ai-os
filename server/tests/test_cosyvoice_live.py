"""真实合成、PCM 格式检查、独立 ASR 回读及 Core 任务验收。"""
import base64
import io
import os
import subprocess
import uuid
import wave
from pathlib import Path
import httpx
import pytest
from homeai.contracts import ProviderManifest
from homeai.db import Provider, Secret, Principal, scope, uid
from homeai.runtime import run_task
from test_workflows import workflow

pytestmark=pytest.mark.skipif(os.getenv('HOMEAI_COSYVOICE_TEST')!='1',reason='需要真实 CosyVoice、FunASR 和 ffmpeg')


def invoke(operation,arguments):
    token=Path('state/provider-secrets/cosyvoice.token').read_text().strip()
    with httpx.Client(trust_env=False,timeout=300) as client:
        return client.post('http://127.0.0.1:8106/invoke/'+operation,headers={'Authorization':'Bearer '+token},
            json={'subject_id':'cosyvoice-acceptance','invocation_id':str(uuid.uuid4()),'arguments':arguments})


@pytest.mark.parametrize('speaker,text,language,keywords',[
    ('中文女','你好，欢迎使用家庭助手。','zh',['欢迎','家庭助手']),
    ('英文女','Welcome to your home assistant.','en',['welcome','home','assistant']),
])
def test_real_audio_roundtrip(tmp_path,speaker,text,language,keywords):
    response=invoke('synthesize',{'text':text,'speaker':speaker})
    assert response.status_code==200,response.text
    result=response.json();audio=base64.b64decode(result['audio_base64'],validate=True)
    with wave.open(io.BytesIO(audio),'rb') as stream:
        assert stream.getnchannels()==1 and stream.getsampwidth()==2
        assert stream.getframerate()==22050
        assert 0.2<stream.getnframes()/stream.getframerate()<=45
        assert any(stream.readframes(stream.getnframes()))
    source=tmp_path/'generated.wav';target=tmp_path/'asr-16k.wav';source.write_bytes(audio)
    subprocess.run(['ffmpeg','-nostdin','-loglevel','error','-y','-i',str(source),'-ar','16000','-ac','1','-c:a','pcm_s16le',str(target)],check=True)
    token=Path('state/provider-secrets/funasr.token').read_text().strip()
    with httpx.Client(trust_env=False,timeout=180) as client:
        response=client.post('http://127.0.0.1:8104/invoke/transcribe',headers={'Authorization':'Bearer '+token},
            json={'subject_id':'cosyvoice-acceptance','invocation_id':str(uuid.uuid4()),'arguments':{
                'content_base64':base64.b64encode(target.read_bytes()).decode(),'language':language}})
        assert response.status_code==200,response.text
        recognized=response.json()['text'].lower()
        assert all(word in recognized for word in keywords),recognized


def test_voices_authentication_and_clone_parameters_rejected():
    with httpx.Client(trust_env=False,timeout=10) as client:
        assert client.get('http://127.0.0.1:8106/health').status_code==401
    result=invoke('voices',{}).json()
    assert '中文女' in result['voices'] and result['voice_cloning'] is False
    for args in [{'text':'你好','speaker':'中文女','prompt_audio':'invalid'},
                 {'text':'你好','speaker':'unknown'},
                 {'text':'x'*301,'speaker':'中文女'},
                 {'text':'<|endofprompt|>你好','speaker':'中文女'}]:
        assert invoke('synthesize',args).status_code==422


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv('HOMEAI_INTEGRATION')!='1',reason='需要真实 PostgreSQL/OPA')
async def test_core_synthesis_and_voice_list(workflow):
    app,user=workflow;provider_id='a-cosyvoice.'+uuid.uuid4().hex
    token=Path('state/provider-secrets/cosyvoice.token').read_text().strip()
    with app.db() as db:
        principal=db.get(Principal,user.user_id);scope(db,user.user_id,principal.household_id);secret_id=uid()
        db.add(Secret(id=secret_id,owner_id=user.user_id,household_id=principal.household_id,provider_id=provider_id,
            value=app.vault.seal(token,user.user_id+':secret:'+secret_id)))
        manifest=ProviderManifest(id=provider_id,version='fbb71de2afe387ed854eebd80b9f3d078c6b9869',adapter='http',
            endpoint='http://127.0.0.1:8106',allowed_hosts=['127.0.0.1'],secret_id=secret_id,timeout_seconds=300,
            capabilities={'speech.synthesize@v1':'/invoke/synthesize','speech.voices@v1':'/invoke/voices'})
        db.add(Provider(id=provider_id,manifest=manifest.model_dump_json(),enabled=True));db.commit()
    try:
        for capability,args in [('speech.voices@v1',{}),('speech.synthesize@v1',{'text':'你好，家庭助手已准备好。','speaker':'中文女'})]:
            response=user.request('POST','/api/v1/tasks',{'capability':capability,'arguments':args,'step_timeout_seconds':300,'idempotency_key':str(uuid.uuid4())})
            assert response.status_code==202,response.text
            tid=response.json()['id'];await run_task(app,tid,user.user_id)
            result=user.request('GET','/api/v1/tasks/'+tid).json()
            assert result['status']=='SUCCEEDED',result
            if capability=='speech.voices@v1':assert '中文女' in result['result']['voices']
            else:
                audio=base64.b64decode(result['result']['audio_base64'],validate=True)
                with wave.open(io.BytesIO(audio),'rb') as stream:assert stream.getnframes()>0
    finally:
        with app.db() as db:db.get(Provider,provider_id).enabled=False;db.commit()
