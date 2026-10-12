"""语音通话隔离、生命周期、频率、音频及真实状态机；HTTP替身仅用于自动测试。"""
import base64
import io
import json
import uuid
import wave
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from conftest import SignedClient
from homeai.contracts import ProviderManifest
from homeai.db import Provider, Task, scope, now
from homeai.providers import Registry
from homeai.security import Actor
from homeai.voice_calls import VoiceCall, VoiceTurn, VoIPRegistration, create_call, checked_call
from homeai.voice_worker import process_turn, cycle
from homeai.runtime import run_task


def wav():
    buffer=io.BytesIO()
    with wave.open(buffer,'wb') as stream:
        stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000);stream.writeframes(b'\x10\x00'*3200)
    return base64.b64encode(buffer.getvalue()).decode()


def setup(system, monkeypatch):
    app=system[0].state
    manifest=ProviderManifest(id='voice.test',version='1',adapter='http',endpoint='http://voice.test',allowed_hosts=['voice.test'],capabilities={
        'speech.transcribe@v1':'/transcribe','speech.synthesize@v1':'/synthesize','speech.voices@v1':'/voices'})
    model=ProviderManifest(id='model.test',version='1',adapter='openai',endpoint='http://model.test/v1',model='test',allowed_hosts=['model.test'],capabilities={'model.generate@v1':'chat'})
    with app.db() as db:
        for item in (manifest,model):db.add(Provider(id=item.id,manifest=item.model_dump_json(),enabled=True))
        db.commit()
    def transport(request):
        if request.url.path.endswith('transcribe'):return httpx.Response(200,json={'text':'公开语音测试'})
        if request.url.path.endswith('synthesize'):return httpx.Response(200,json={'audio_base64':wav(),'sample_rate':16000,'format':'wav'})
        if json.loads(request.content).get('response_format',{}).get('json_schema',{}).get('name')=='message_route':
            return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':json.dumps({'simple':True,'requires_tools':False,'acknowledgement':''})}}],'usage':{'total_tokens':8}})
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'这是公开答复。'}}],'usage':{'total_tokens':8}})
    app.registry=Registry(app.vault,httpx.MockTransport(transport))
    monkeypatch.setattr('homeai.notifications_config.resolve_configuration',lambda app:SimpleNamespace(status='ready'))
    return app


def actor(alice):return Actor(alice.user_id,'h1',alice.device_id,'infrastructure_owner')
def key():return {'client_key':str(uuid.uuid4())}
def start(alice):
    response=alice.request('POST','/api/v1/voice/calls',key());assert response.status_code==201,response.text
    return response.json()['id']


def test_disabled_speech_cannot_fake_call(system,alice):
    assert alice.request('POST','/api/v1/voice/calls',key()).status_code==503


def test_call_creation_idempotency_and_isolation(system,alice,monkeypatch):
    setup(system,monkeypatch);body=key()
    response=alice.request('POST','/api/v1/voice/calls',body);identifier=response.json()['id']
    assert alice.request('POST','/api/v1/voice/calls',body).json()['id']==identifier
    assert alice.request('POST','/api/v1/voice/calls',key()).status_code==409
    bob=SignedClient(system[1],system[2])
    assert bob.request('GET','/api/v1/voice/calls/'+identifier).status_code==404
    assert bob.request('POST','/api/v1/voice/calls/'+identifier+'/end').status_code==404
    assert alice.request('POST','/api/v1/voice/calls/'+identifier+'/end').json()['status']=='ENDED'
    assert alice.request('POST','/api/v1/voice/calls/'+identifier+'/accept').status_code==409


def test_audio_validation_and_idempotency(system,alice,monkeypatch):
    setup(system,monkeypatch);identifier=start(alice);path='/api/v1/voice/calls/'+identifier+'/turns'
    assert alice.request('POST',path,key()|{'content_base64':'not-audio'}).status_code==422
    body=key()|{'content_base64':wav()}
    response=alice.request('POST',path,body);assert response.status_code==202,response.text
    assert alice.request('POST',path,body).json()['id']==response.json()['id']
    assert alice.request('POST',path,key()|{'content_base64':wav()}).status_code==409
    alice.request('POST','/api/v1/voice/calls/'+identifier+'/end')
    assert alice.request('POST',path,body).status_code==409


def register(alice):
    assert alice.request('PUT','/api/v1/voice/device',{'token':'ab'*32,'environment':'sandbox'}).status_code==200


def test_incoming_optin_quiet_hours_rate_limit_and_no_plain_token(system,alice,monkeypatch):
    app=setup(system,monkeypatch);register(alice)
    with app.db() as db:
        scope(db,alice.user_id,'h1')
        registration=db.scalar(select(VoIPRegistration));assert 'ab'*32 not in registration.token
        with pytest.raises(Exception) as error:create_call(app,db,actor(alice),'model-call',incoming=True)
        assert error.value.status_code==403
    assert alice.request('PUT','/api/v1/voice/settings',{'incoming_enabled':True,'quiet_start':0,'quiet_end':0}).status_code==200
    with app.db() as db:
        scope(db,alice.user_id,'h1');call=create_call(app,db,actor(alice),'model-call',incoming=True);db.commit()
        assert call.status=='RINGING'
    alice.request('POST','/api/v1/voice/calls/'+call.id+'/end')
    with app.db() as db:
        with pytest.raises(Exception) as error:create_call(app,db,actor(alice),'another-call',incoming=True)
        assert error.value.status_code==429
    alice.request('PUT','/api/v1/voice/settings',{'incoming_enabled':True,'quiet_start':0,'quiet_end':23,'timezone':'UTC'})
    monkeypatch.setattr('homeai.voice_calls.now',lambda:1790935200.0)
    with app.db() as db:
        with pytest.raises(Exception) as error:create_call(app,db,actor(alice),'quiet-call',incoming=True)
        assert error.value.status_code==409


@pytest.mark.asyncio
async def test_apns_unknown_result_is_not_replayed(system,alice,monkeypatch):
    app=setup(system,monkeypatch);register(alice)
    identifier=alice.request('POST','/api/v1/voice/test-call',key()).json()['id']
    sent=[]
    class Broken:
        async def send_voip(self,*args):sent.append(args);raise httpx.ReadTimeout('test')
    await cycle(app,Broken());await cycle(app,Broken())
    assert len(sent)==1
    assert alice.request('GET','/api/v1/voice/calls/'+identifier).json()['push_status']=='UNKNOWN'


@pytest.mark.asyncio
async def test_voice_turn_reuses_server_conversation_and_returns_real_task_audio(system,alice,monkeypatch):
    app=setup(system,monkeypatch);identifier=start(alice)
    turn_id=alice.request('POST','/api/v1/voice/calls/'+identifier+'/turns',key()|{'content_base64':wav()}).json()['id']
    for _ in range(12):
        with app.db() as db:
            scope(db,alice.user_id,'h1')
            tasks=list(db.scalars(select(Task.id).where(Task.owner_id==alice.user_id,Task.status.in_(['RECEIVED','EXECUTING']))))
        for task in tasks:await run_task(app,task,alice.user_id)
        process_turn(app,actor(alice),turn_id)
        response=alice.request('GET','/api/v1/voice/calls/'+identifier+'/turns/'+turn_id)
        if response.json()['status']=='READY':break
    assert response.json()['status']=='READY',response.text
    assert response.json()['transcript']=='公开语音测试'
    assert response.json()['answer']=='这是公开答复。'
    audio=alice.request('GET','/api/v1/voice/calls/'+identifier+'/turns/'+turn_id+'/audio/0')
    assert audio.status_code==200 and audio.json()['audio_base64']==wav()
    conversation=alice.request('POST','/api/v1/conversations/default').json()
    assert conversation['id']
    with app.db() as db:
        row=db.get(VoiceTurn,turn_id);assert '公开语音测试' not in row.payload


def test_wrong_device_cannot_answer_or_read(system,alice,monkeypatch):
    app=setup(system,monkeypatch);identifier=start(alice)
    with app.db() as db:
        with pytest.raises(Exception) as error:checked_call(app,db,Actor(alice.user_id,'h1','other-device','adult'),identifier)
        assert error.value.status_code==403


def test_expired_call_and_optout_cannot_be_answered(system,alice,monkeypatch):
    app=setup(system,monkeypatch);register(alice)
    identifier=alice.request('POST','/api/v1/voice/test-call',key()).json()['id']
    with app.db() as db:
        scope(db,alice.user_id,'h1');db.get(VoiceCall,identifier).expires_at=now()-1;db.commit()
    assert alice.request('POST','/api/v1/voice/calls/'+identifier+'/accept').status_code==409
    alice.request('GET','/api/v1/voice/calls/'+identifier)
    another=alice.request('POST','/api/v1/voice/test-call',key()).json()['id']
    alice.request('PUT','/api/v1/voice/settings',{'incoming_enabled':False})
    assert alice.request('POST','/api/v1/voice/calls/'+another+'/accept').status_code==409


def test_accept_is_idempotent_and_greeting_has_no_microphone_request(system,alice,monkeypatch):
    app=setup(system,monkeypatch);register(alice)
    identifier=alice.request('POST','/api/v1/voice/test-call',key()).json()['id']
    first=alice.request('POST','/api/v1/voice/calls/'+identifier+'/accept').json()
    second=alice.request('POST','/api/v1/voice/calls/'+identifier+'/accept').json()
    assert first['greeting_turn_id']==second['greeting_turn_id']
    with app.db() as db:
        scope(db,alice.user_id,'h1')
        turns=list(db.scalars(select(VoiceTurn).where(VoiceTurn.call_id==identifier)))
        assert len(turns)==1 and turns[0].status=='SYNTHESIZING'
        payload=app.vault.open(turns[0].payload,alice.user_id+':voice-turn:'+turns[0].id)
        assert 'asr_task' not in payload and len(payload['tts_tasks'])==1


@pytest.mark.asyncio
async def test_voip_headers_and_payload_contain_only_call_identifier(system):
    from homeai.notifications import APNs
    sent=[]
    client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request:(sent.append(request) or httpx.Response(200))))
    apns=APNs(system[0].state.settings,client)
    apns.configuration=SimpleNamespace(topic='dev.homeai.ios')
    async def refresh():return 'ready'
    apns.refresh=refresh;apns.authorization=lambda:'test-auth'
    identifier=str(uuid.uuid4())
    await apns.send_voip('ab'*32,'sandbox',identifier)
    assert sent[0].headers['apns-topic']=='dev.homeai.ios.voip'
    assert sent[0].headers['apns-push-type']=='voip' and sent[0].headers['apns-expiration']=='0'
    assert json.loads(sent[0].content)=={'aps':{},'call_id':identifier}
    await client.aclose()


def test_real_voice_rls():
    import os
    if os.environ.get('HOMEAI_INTEGRATION')!='1':pytest.skip('需要独立PostgreSQL测试库')
    from homeai.config import Settings
    from homeai.api import create_app
    from fastapi.testclient import TestClient
    from homeai.voice_calls import VoicePreferences
    settings=Settings();settings.database_url=settings.database_url.rsplit('/',1)[0]+'/homeai_test'
    app=create_app(settings);client=TestClient(app)
    household=str(uuid.uuid4());alice=SignedClient(client,app.state.db,household=household);bob=SignedClient(client,app.state.db,household=household)
    assert alice.request('PUT','/api/v1/voice/settings',{'incoming_enabled':True}).status_code==200
    with app.state.db() as db:
        scope(db,alice.user_id,household);identifier=db.scalar(select(VoicePreferences.id).where(VoicePreferences.owner_id==alice.user_id))
        assert identifier
    with app.state.db() as db:
        assert db.get(VoicePreferences,identifier) is None
        scope(db,bob.user_id,household);assert db.get(VoicePreferences,identifier) is None
