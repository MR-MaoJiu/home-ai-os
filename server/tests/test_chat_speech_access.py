"""朗读使用同一聊天投影，不以任务完成与否替代来源授权检查。"""
import uuid

import pytest

from conftest import SignedClient
from homeai.db import Task, scope
from test_conversations import new_chat, send
from test_security_data import put, record


def prepare(system, alice, *, status='SUCCEEDED', result=None, dependencies=None):
    conversation=new_chat(alice)
    turn=send(alice,conversation,'公开朗读验证').json()
    identifier=turn['task_id']
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        task=db.get(Task,identifier)
        app=system[0].state
        payload=app.vault.open(task.request,alice.user_id+':task:'+identifier)
        payload['_chat_ack']='好，我来查一下。'
        payload['_client_message_version']='2.0'
        if dependencies:payload['_record_dependencies']=dependencies
        task.request=app.vault.seal(payload,alice.user_id+':task:'+identifier)
        task.status=status
        if result is not None:task.result=app.vault.seal(result,alice.user_id+':task-result:'+identifier)
        db.commit()
    return conversation,identifier


def read(alice,identifier):
    return alice.request('GET','/api/v1/tasks/'+identifier+'/chat-speech')


@pytest.mark.parametrize('status', ['RECEIVED','EXECUTING','WAITING_CLIENT','WAITING_PRIVACY','AWAITING_APPROVAL'])
def test_acknowledgement_can_be_read_before_completion_without_exposing_tool_receipts(system,alice,status):
    _,identifier=prepare(system,alice,status=status,result={'status':'stored','record_id':'not-a-chat-answer'})
    response=read(alice,identifier)
    assert response.status_code==200,response.text
    value=response.json()
    assert value['acknowledgement']=='好，我来查一下。'
    assert value['assistant_text']==''
    assert value['result_redacted'] is False
    assert value['status']==status
    assert response.headers['cache-control']=='no-store, private'
    assert value['approvals']==[] and value['client_actions']==[]
    assert alice.request('GET','/api/v1/tasks/'+identifier+'/content-access').json()['allowed'] is False


@pytest.mark.parametrize('status,answer', [
    ('SUCCEEDED','回答已经完成。'),('FAILED','这次没有查到可用结果。'),
    ('NEEDS_RECONCILIATION','操作结果还需要核对。'),('CANCELED','任务已停止。')])
def test_chat_answer_remains_readable_in_each_terminal_state(system,alice,status,answer):
    conversation,identifier=prepare(system,alice,status=status,result={'text':answer,
        'parts':[{'type':'text','text':'这里还有一段说明。'}],
        'sources':[]})
    actual=read(alice,identifier).json()
    projected=alice.request('GET','/api/v1/conversations/'+conversation).json()['turns'][0]
    for field in ('assistant_text','assistant_parts','acknowledgement','sources','status','display_revision'):
        assert actual[field]==projected[field]
    assert actual['assistant_text']==answer
    assert actual['result_redacted'] is False


@pytest.mark.parametrize('status,answer', [('FAILED','没办成，原因在任务详情里。'),('CANCELED','任务已停止。')])
def test_failure_and_cancellation_messages_do_not_need_success_results(system,alice,status,answer):
    _,identifier=prepare(system,alice,status=status)
    assert read(alice,identifier).json()['assistant_text']==answer


def test_streaming_segments_are_visible_but_never_revived_after_failure(system,alice):
    _,identifier=prepare(system,alice,status='EXECUTING',result={'text':'第一句。','_streaming':True})
    value=read(alice,identifier).json()
    assert value['assistant_text']=='第一句。'
    assert value['requires_online_revalidation'] is True
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        db.get(Task,identifier).status='FAILED'
        db.commit()
    value=read(alice,identifier).json()
    assert '第一句' not in value['assistant_text']
    assert value['requires_online_revalidation'] is False


def test_guessed_tasks_and_tasks_without_chat_are_not_readable(system,alice):
    _,identifier=prepare(system,alice,result={'text':'只属于当前成员'})
    bob=SignedClient(system[1],system[2])
    outsider=SignedClient(system[1],system[2],household='h2')
    assert read(bob,identifier).status_code==404
    assert read(outsider,identifier).status_code==404
    assert read(alice,str(uuid.uuid4())).status_code==404
    detached=alice.request('POST','/api/v1/tasks',{'idempotency_key':str(uuid.uuid4()),'message':'独立任务'}).json()['id']
    assert read(alice,detached).status_code==404


@pytest.mark.parametrize('change', ['revoke','delete','version','secret'])
def test_source_changes_hide_old_acknowledgement_and_all_answer_parts(system,alice,change):
    bob=SignedClient(system[1],system[2])
    source=put(bob,record(source_id='speech-source',payload={'text':'授权测试来源'}))
    assert bob.request('PUT','/api/v1/data/'+source+'/visibility',{'visibility':'family'}).status_code==200
    _,identifier=prepare(system,alice,result={'text':'不能在撤权后朗读的旧正文',
        'parts':[{'type':'text','text':'不能在撤权后朗读的另一段'}],
        'sources':[{'record_id':source,'version':1,'title':'来源','task_id':'source-task'}]},
        dependencies={source:1})
    assert read(alice,identifier).json()['result_redacted'] is False
    if change=='revoke':
        assert bob.request('PUT','/api/v1/data/'+source+'/visibility',{'visibility':'personal'}).status_code==200
    elif change=='delete':
        assert bob.request('DELETE','/api/v1/data/'+source).status_code==200
    else:
        from homeai.db import Record
        with system[2]() as db:
            scope(db,bob.user_id,'h1')
            item=db.get(Record,source)
            if change=='version':item.version+=1
            else:item.sensitivity='SECRET'
            db.commit()
    value=read(alice,identifier).json()
    assert value['result_redacted'] is True
    assert value['assistant_text']=='关联资料已删除或撤权，旧回答已隐藏。'
    assert value['acknowledgement']==''
    assert value['assistant_parts']==[] and value['sources']==[]
    assert '不能在撤权后朗读' not in str(value)


def test_reading_does_not_change_task_state_or_payload(system,alice):
    _,identifier=prepare(system,alice,status='WAITING_CLIENT')
    def snapshot():
        with system[2]() as db:
            scope(db,alice.user_id,'h1')
            task=db.get(Task,identifier)
            return task.status,task.request,task.result,task.cancel_requested
    original=snapshot()
    assert read(alice,identifier).status_code==200
    assert snapshot()==original


@pytest.mark.parametrize('change',['expired','revoked'])
def test_expired_or_revoked_task_consent_stops_reading_cached_answer(system,alice,change):
    from homeai.client_actions import ClientAction
    from homeai.db import now
    from test_client_actions import request_action,response
    from test_conversations import finish
    _,identifier=prepare(system,alice,status='EXECUTING')
    action_id=request_action(system,alice,identifier,'cloud.disclose',
        {'scope_hash':'a'*64,'record_versions':{},'provider_id':'public-test'})
    assert response(alice,action_id).status_code==200
    finish(system,alice,identifier,'只在本次授权有效时可读的回答')
    assert read(alice,identifier).json()['result_redacted'] is False
    if change=='revoked':
        assert alice.request('POST','/api/v1/client-actions/'+action_id+'/revoke').status_code==200
    else:
        with system[2]() as db:
            scope(db,alice.user_id,'h1')
            db.get(ClientAction,action_id).expires_at=now()-1
            db.commit()
    value=read(alice,identifier).json()
    assert value['result_redacted'] is True
    assert '只在本次授权' not in value['assistant_text']
    assert value['acknowledgement']==''
