"""交互语音只在原界面反馈，不污染通知箱；资料校验使用原任务通道。"""
import uuid
import pytest
from sqlalchemy import select
from conftest import SignedClient
from homeai.db import Task,Record,Consumption,Notification,PushDelivery,scope,uid
from homeai.data import emit
from homeai.security import Actor
from homeai.notifications import collect_task_events,create_notification,deliver
from test_notifications import register


def speech_task(system,alice,capability,status='SUCCEEDED',automation=False):
    response=alice.request('POST','/api/v1/tasks',{'capability':capability,'idempotency_key':str(uuid.uuid4()),'arguments':{}})
    task_id=response.json()['id'];app=system[0].state
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,task_id);task.status=status
        if automation:
            body=app.vault.open(task.request,alice.user_id+':task:'+task_id);body['_automation_id']='scheduled-task'
            task.request=app.vault.seal(body,alice.user_id+':task:'+task_id)
        emit(db,Actor(alice.user_id,'h1',alice.device_id,'adult'),'task.updated',task_id);db.commit()
    return task_id


@pytest.mark.parametrize('capability',['speech.voices@v1','speech.synthesize@v1','speech.transcribe@v1'])
@pytest.mark.parametrize('status',['SUCCEEDED','FAILED','CANCELED'])
def test_interactive_speech_is_consumed_without_notification(system,alice,capability,status):
    register(alice);speech_task(system,alice,capability,status)
    collect_task_events(system[0].state,alice.user_id,'h1');collect_task_events(system[0].state,alice.user_id,'h1')
    with system[2]() as db:
        assert list(db.scalars(select(Consumption)))
        assert not list(db.scalars(select(Notification)))
        assert not list(db.scalars(select(PushDelivery)))


def test_scheduled_speech_still_notifies(system,alice):
    speech_task(system,alice,'speech.synthesize@v1',automation=True)
    collect_task_events(system[0].state,alice.user_id,'h1')
    assert len(alice.request('GET','/api/v1/notifications').json()['items'])==1


@pytest.mark.asyncio
async def test_old_queued_interactive_speech_push_is_not_sent(system,alice):
    register(alice);task_id=speech_task(system,alice,'speech.synthesize@v1')
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        create_notification(db,'h1',alice.user_id,'old-speech','task.updated','personal','SUCCEEDED',task_id=task_id)
        db.commit()
    class NoSend:
        async def refresh(self):return 'ready'
        async def send(self,*args):pytest.fail('交互语音不能再发送旧排队推送')
    await deliver(system[0].state,NoSend(),alice.user_id,'h1')
    with system[2]() as db:assert db.scalar(select(PushDelivery)).status=='canceled'


def test_android_source_is_readable_via_original_task_but_not_memory(system,alice):
    app=system[0].state
    task_id=speech_task(system,alice,'speech.voices@v1')
    identifier=uid()
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        db.add(Record(id=identifier,owner_id=alice.user_id,household_id='h1',source='android_observation',source_id=uid(),kind='client_input.android_screen',sensitivity='PRIVATE',cloud_policy='LOCAL_ONLY',version=1,
            payload=app.vault.seal({'text':'公开测试页面'},alice.user_id+':record:'+identifier)))
        db.commit()
    assert alice.request('GET','/api/v1/data/'+identifier).status_code==404
    assert alice.request('GET','/api/v1/memory/entries/'+identifier).status_code==404
    path='/api/v1/tasks/'+task_id+'/data/'+identifier
    assert alice.request('GET',path).status_code==200
    bob=SignedClient(system[1],system[2]);assert bob.request('GET',path).status_code==404
    with system[2]() as db:
        db.get(Record,identifier).deleted=True;db.commit()
    assert alice.request('GET',path).status_code==404


def test_plain_chat_has_no_system_push_but_tool_task_reports_completion(system,alice):
    from test_conversations import new_chat,send,finish
    chat=new_chat(alice)
    plain=send(alice,chat,'普通聊天公开测试').json()['task_id']
    business=send(alice,chat,'打开安卓应用公开测试').json()['task_id']
    for task_id in (plain,business):
        finish(system,alice,task_id,'已完成公开测试')
        with system[2]() as db:
            scope(db,alice.user_id,'h1')
            emit(db,Actor(alice.user_id,'h1',alice.device_id,'adult'),'task.updated',task_id);db.commit()
    collect_task_events(system[0].state,alice.user_id,'h1')
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        rows=list(db.scalars(select(Notification)))
        assert [row.task_id for row in rows]==[business]


@pytest.mark.asyncio
async def test_offline_tts_reports_service_unavailable_without_notification(system,alice):
    import httpx
    from homeai.db import Provider
    from homeai.providers import Registry
    from homeai.contracts import ProviderManifest
    from homeai.runtime import run_task
    app=system[0].state
    def offline(request):raise httpx.ConnectError('test-only-endpoint-detail',request=request)
    app.registry=Registry(app.vault,httpx.MockTransport(offline))
    with system[2]() as db:
        manifest=ProviderManifest(id='test.offline-tts',version='1',adapter='http',endpoint='http://127.0.0.1:8106',allowed_hosts=['127.0.0.1'],capabilities={'speech.voices@v1':'/invoke/voices'})
        db.add(Provider(id=manifest.id,manifest=manifest.model_dump_json(),enabled=True));db.commit()
    task=alice.request('POST','/api/v1/tasks',{'capability':'speech.voices@v1','arguments':{},'idempotency_key':'offline-voice-test'}).json()['id']
    await run_task(app,task,alice.user_id)
    await run_task(app,task,alice.user_id)
    result=alice.request('GET','/api/v1/tasks/'+task).json()
    assert result['status']=='FAILED' and '连接不到' in result['error']
    assert 'test-only-endpoint-detail' not in result['error']
    collect_task_events(app,alice.user_id,'h1')
    assert alice.request('GET','/api/v1/notifications').json()['items']==[]
