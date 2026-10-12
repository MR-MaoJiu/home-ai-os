import json,uuid
import pytest
from conftest import SignedClient
from homeai.db import Task,ConversationTurn,scope
from homeai.conversations import history,import_legacy
from homeai.security import Actor
from test_security_data import put,record


def new_chat(client):
    result=client.request('POST','/api/v1/conversations',{'client_id':str(uuid.uuid4())})
    assert result.status_code==200,result.text
    return result.json()['id']

def send(client,identifier,content,key=None):
    return client.request('POST','/api/v1/conversations/'+identifier+'/messages',{'client_key':key or str(uuid.uuid4()),'content':content})

def finish(system,client,task_id,text):
    with system[2]() as db:
        scope(db,client.user_id,'h1');task=db.get(Task,task_id);task.status='SUCCEEDED'
        task.result=system[0].state.vault.seal({'choices':[{'message':{'content':text}}]},client.user_id+':task-result:'+task.id);db.commit()


def test_persistent_chat_idempotence_scope_and_context(system,alice):
    identifier=new_chat(alice);key=str(uuid.uuid4())
    first=send(alice,identifier,'本次对话代号青竹42',key)
    assert first.status_code==202,first.text
    task=first.json()['task_id']
    assert send(alice,identifier,'本次对话代号青竹42',key).json()['id']==first.json()['id']
    assert send(alice,identifier,'不同文本',key).status_code==409
    assert send(alice,identifier,'并发下一条').status_code==202
    bob=SignedClient(system[1],system[2])
    assert bob.request('GET','/api/v1/conversations/'+identifier).status_code==404
    assert bob.request('GET','/api/v1/conversations').json()==[]
    finish(system,alice,task,'收到，代号是青竹42。')
    second=send(alice,identifier,'刚才的代号是什么？')
    assert second.status_code==202
    # 新客户端重新读取服务器，不依赖任何客户端内存。
    page=alice.request('GET','/api/v1/conversations/'+identifier).json()
    assert len(page['turns'])==3 and page['turns'][0]['assistant_text']=='收到，代号是青竹42。'
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,second.json()['task_id'])
        body=system[0].state.vault.open(task.request,alice.user_id+':task:'+task.id)
        actor=Actor(alice.user_id,'h1',alice.device_id,'infrastructure_owner')
        messages=history(system[0].state,db,actor,body)
        assert [m['role'] for m in messages]==['user','assistant','user','assistant'] and '青竹42' in messages[0]['content']
        assert messages[2]['content']=='并发下一条' and '尚未返回结果' in messages[3]['content']
    assert send(bob,identifier,'越权').status_code==404
    assert alice.request('POST','/api/v1/conversations/'+identifier+'/messages',{'client_key':str(uuid.uuid4()),'content':'x','steps':[]}).status_code==422


def test_privacy_error_projection_matches_chat_and_task_without_rewriting_history(system,alice):
    identifier=new_chat(alice)
    submitted=send(alice,identifier,'公开测试：核对本次提示').json()
    task_id=submitted['task_id']
    original='本地隐私模型已停用或尚未通过能力验证'
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        task=db.get(Task,task_id)
        body=system[0].state.vault.open(task.request,alice.user_id+':task:'+task_id)
        body['_privacy_failure']={'code':'privacy_unavailable','stage':'detect','reason':original}
        task.request=system[0].state.vault.seal(body,alice.user_id+':task:'+task_id)
        task.status,task.error='WAITING_PRIVACY',original
        db.commit()
    task_view=alice.request('GET','/api/v1/tasks/'+task_id).json()
    chat_view=alice.request('GET','/api/v1/conversations/'+identifier).json()['turns'][0]
    assert task_view['error']==chat_view['error']=='本地隐私模型当前不可用'
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        assert db.get(Task,task_id).error==original


def test_legacy_import_is_idempotent(system,alice):
    task=alice.request('POST','/api/v1/tasks',{'idempotency_key':str(uuid.uuid4()),'message':'旧单轮提问'}).json()['id']
    finish(system,alice,task,'旧回答')
    with system[2]() as db:
        actor=Actor(alice.user_id,'h1',alice.device_id,'infrastructure_owner')
        assert import_legacy(system[0].state,db,actor)==1
        assert import_legacy(system[0].state,db,actor)==0
    identifier=alice.request('GET','/api/v1/conversations').json()[0]['id']
    assert alice.request('GET','/api/v1/conversations/'+identifier).json()['turns'][0]['assistant_text']=='旧回答'


def test_own_cloud_document_followup_keeps_sources_but_never_reuses_consent(system,alice):
    from test_media import upload
    from homeai.client_actions import create_request,ClientAction
    from homeai.db import now
    asset=upload(alice,b'CODE-91\nsecond line','followup.txt','file').json()['asset']
    identifier=new_chat(alice)
    first=alice.request('POST','/api/v1/conversations/'+identifier+'/messages',{'client_key':str(uuid.uuid4()),'content':'总结文件','parts':[{'type':'file','record_id':asset['record_id'],'version':1}]}).json()
    actor=Actor(alice.user_id,'h1',alice.device_id,'infrastructure_owner');app=system[0].state
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,first['task_id'])
        action=create_request(app,db,actor,task,'cloud.disclose','本轮云披露',{'scope_hash':'b'*64,'record_versions':{asset['record_id']:1},'provider_id':'test.model'})
        action_id=action['client_action_id'];db.commit()
    assert alice.request('POST','/api/v1/client-actions/'+action_id+'/respond',{'idempotency_key':'followup-approval'}).status_code==200
    finish(system,alice,first['task_id'],'这是本人的上轮文件摘要')
    second=send(alice,identifier,'刚才的文件第二行是什么？').json()
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,second['task_id'])
        body=app.vault.open(task.request,alice.user_id+':task:'+task.id)
        prior=history(app,db,actor,body)
        assert '这是本人的上轮文件摘要' in json.dumps(prior,ensure_ascii=False)
        assert asset['record_id'] in body['_historical_record_ids']
        assert '_cloud_consents' not in body
        # 云披露批准失效后仍可重新引用本人的原文件，但不能把旧摘要当作新证据。
        db.get(ClientAction,action_id).expires_at=now()-1;db.commit()
        body=app.vault.open(task.request,alice.user_id+':task:'+task.id)
        prior=history(app,db,actor,body)
        assert '这是本人的上轮文件摘要' not in json.dumps(prior,ensure_ascii=False)
        assert asset['record_id'] in body['_historical_record_ids']


def test_history_does_not_revive_revoked_sources(system,alice):
    bob=SignedClient(system[1],system[2]);rid=put(bob,record(payload={'title':'共享资料'}))
    assert bob.request('PUT','/api/v1/data/'+rid+'/visibility',{'visibility':'family'}).status_code==200
    identifier=new_chat(alice);first=send(alice,identifier,'读取共享资料').json()
    finish(system,alice,first['task_id'],'来自共享来源的旧回答')
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,first['task_id'])
        value=system[0].state.vault.open(task.request,alice.user_id+':task:'+task.id)
        value['_record_dependencies']={rid:1};task.request=system[0].state.vault.seal(value,alice.user_id+':task:'+task.id);db.commit()
    bob.request('PUT','/api/v1/data/'+rid+'/visibility',{'visibility':'personal'})
    page=alice.request('GET','/api/v1/conversations/'+identifier).json()
    assert '旧回答已隐藏' in page['turns'][0]['assistant_text']
    second=send(alice,identifier,'继续').json()
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,second['task_id'])
        payload=system[0].state.vault.open(task.request,alice.user_id+':task:'+task.id)
        assert history(system[0].state,db,Actor(alice.user_id,'h1',alice.device_id,'infrastructure_owner'),payload)==[]


def test_intent_is_orchestrated_by_server_and_history_is_available(system,alice):
    body={'idempotency_key':str(uuid.uuid4()),'title':'来自快捷入口的提醒'}
    first=alice.request('POST','/api/v1/input/reminder',body)
    assert first.status_code==202,first.text
    assert alice.request('POST','/api/v1/input/reminder',body).json()==first.json()
    page=alice.request('GET','/api/v1/conversations/'+first.json()['conversation_id']).json()
    assert page['turns'][0]['task_id']==first.json()['id']
    assert alice.request('POST','/api/v1/conversations/'+first.json()['conversation_id']+'/messages',{'client_key':str(uuid.uuid4()),'content':'x','timezone':'not/a/zone'}).status_code==422


def test_default_conversation_reuses_existing_and_stays_stable(system,alice):
    previous=new_chat(alice)
    first=send(alice,previous,'已有内容').json()
    finish(system,alice,first['task_id'],'已有回答')
    result=alice.request('POST','/api/v1/conversations/default')
    assert result.status_code==200,result.text
    assert result.json()['id']==previous
    new_chat(alice)  # 旧接口保留的其他会话不会替换持续会话。
    assert alice.request('POST','/api/v1/conversations/default').json()['id']==previous
    bob=SignedClient(system[1],system[2])
    assert bob.request('POST','/api/v1/conversations/default').json()['id']!=previous


def test_chat_incremental_updates_and_permission_reset(system,alice):
    from homeai.data import emit
    identifier=alice.request('POST','/api/v1/conversations/default').json()['id']
    first=send(alice,identifier,'第一轮').json()
    page=alice.request('GET','/api/v1/conversations/'+identifier).json()
    url=f"/api/v1/conversations/{identifier}/updates?after=1&etag={page['etag']}"
    unchanged=alice.request('GET',url).json()
    assert unchanged['unchanged'] and not unchanged['reset'] and unchanged['turns']==[]
    finish(system,alice,first['task_id'],'完成回答')
    with system[2]() as db:
        scope(db,alice.user_id,'h1');emit(db,Actor(alice.user_id,'h1',alice.device_id,'adult'),'task.updated',first['task_id']);db.commit()
    updated=alice.request('GET',url).json()
    assert updated['turns'][0]['assistant_text']=='完成回答' and not updated['reset']
    # 权限或来源数据变更必须令整个旧聊天缓存失效，包括不在最近40轮的页。
    put(alice,record(source_id='changed-source'))
    invalidated=alice.request('GET',f"/api/v1/conversations/{identifier}/updates?after=1&etag={updated['etag']}").json()
    assert invalidated['reset'] and invalidated['turns']==[]
    assert invalidated['reset_reason']=='sources_changed'
    assert invalidated['snapshot']['etag']==invalidated['etag']
    assert invalidated['snapshot']['turns'][0]['assistant_text']=='完成回答'
    latest=invalidated['snapshot']
    settled=alice.request('GET',f"/api/v1/conversations/{identifier}/updates?after=1&etag={latest['etag']}").json()
    assert settled['unchanged'] and not settled['reset']
    bob=SignedClient(system[1],system[2])
    assert bob.request('GET',url).status_code==404


def test_source_revocation_reset_contains_only_revalidated_snapshot(system,alice):
    bob=SignedClient(system[1],system[2]);rid=put(bob,record(payload={'title':'待撤回的共享资料'}))
    assert bob.request('PUT','/api/v1/data/'+rid+'/visibility',{'visibility':'family'}).status_code==200
    identifier=new_chat(alice);first=send(alice,identifier,'读取共享资料').json()
    finish(system,alice,first['task_id'],'这段回答依赖即将撤回的资料')
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,first['task_id'])
        value=system[0].state.vault.open(task.request,alice.user_id+':task:'+task.id)
        value['_record_dependencies']={rid:1}
        task.request=system[0].state.vault.seal(value,alice.user_id+':task:'+task.id);db.commit()
    page=alice.request('GET','/api/v1/conversations/'+identifier).json()
    assert not page['turns'][0]['result_redacted']
    assert bob.request('PUT','/api/v1/data/'+rid+'/visibility',{'visibility':'personal'}).status_code==200
    updated=alice.request('GET',f"/api/v1/conversations/{identifier}/updates?after=1&etag={page['etag']}").json()
    assert updated['reset'] and updated['reset_reason']=='sources_changed'
    replacement=updated['snapshot']['turns'][0]
    assert replacement['id']==first['id'] and replacement['user_text']=='读取共享资料'
    assert replacement['result_redacted'] and '旧回答已隐藏' in replacement['assistant_text']
    assert '这段回答依赖即将撤回的资料' not in json.dumps(updated,ensure_ascii=False)


@pytest.mark.parametrize('etag',['','invalid','v1.999999.0'])
def test_invalid_chat_cursor_returns_atomic_page(system,alice,etag):
    identifier=new_chat(alice);first=send(alice,identifier,'已有消息').json()
    finish(system,alice,first['task_id'],'已有答复')
    response=alice.request('GET',f'/api/v1/conversations/{identifier}/updates?after=1&etag={etag}')
    assert response.status_code==200,response.text
    value=response.json()
    assert value['reset'] and value['reset_reason']=='cursor_invalid'
    assert value['snapshot']==alice.request('GET','/api/v1/conversations/'+identifier).json()
    assert value['snapshot']['turns'][0]['assistant_text']=='已有答复'


def test_record_sync_replacements_keep_messages_and_pagination(system,alice):
    identifier=new_chat(alice)
    for index in range(42):send(alice,identifier,'分页公开测试 '+str(index))
    page=alice.request('GET','/api/v1/conversations/'+identifier).json()
    assert page['has_more'] and len(page['turns'])==40 and page['next_before']==3
    ids=[turn['id'] for turn in page['turns']]
    # 连续健康等资料同步会使来源版本失效；每次响应仍带完整可替换的一页，不要求再等一次往返。
    for index in range(3):
        put(alice,record(source_id='sync-change-'+str(index)))
        value=alice.request('GET',f"/api/v1/conversations/{identifier}/updates?after=42&etag={page['etag']}").json()
        assert value['reset'] and value['reset_reason']=='sources_changed'
        page=value['snapshot']
        assert [turn['id'] for turn in page['turns']]==ids
        assert page['has_more'] and page['next_before']==3
    older=alice.request('GET',f'/api/v1/conversations/{identifier}?before={page["next_before"]}').json()
    assert len(older['turns'])==2 and not older['has_more']


def test_chat_snapshot_reads_conversation_after_change_boundary(system,alice,monkeypatch):
    from homeai import sync_order
    from homeai.db import Conversation
    identifier=new_chat(alice);send(alice,identifier,'公开测试')
    observed=[]
    def boundary(db):
        # 等待同步边界期间不能先缓存旧 next_sequence，避免把合法游标判为超前。
        observed.append(not any(isinstance(row,Conversation) for row in db.identity_map.values()))
    monkeypatch.setattr(sync_order,'lock_changes',boundary)
    page=alice.request('GET','/api/v1/conversations/'+identifier).json()
    updates=alice.request('GET',f"/api/v1/conversations/{identifier}/updates?after=1&etag={page['etag']}").json()
    assert not updates['reset'] and observed==[True,True]


def test_chat_snapshot_cursor_does_not_skip_task_event_during_projection(system,alice,monkeypatch):
    from homeai import conversations
    from homeai.data import emit
    identifier=new_chat(alice);first=send(alice,identifier,'公开同步测试').json()
    original=conversations.turn_view
    emitted=False
    def event_during_projection(app,db,actor,turn):
        nonlocal emitted
        if not emitted:
            emitted=True
            # 模拟 Worker 在游标读取后提交任务事件；下一轮必须还能观察到它。
            with system[2]() as writer:
                scope(writer,alice.user_id,'h1')
                emit(writer,Actor(alice.user_id,'h1',alice.device_id,'adult'),'task.updated',first['task_id'])
                writer.commit()
        return original(app,db,actor,turn)
    monkeypatch.setattr(conversations,'turn_view',event_during_projection)
    page=alice.request('GET','/api/v1/conversations/'+identifier).json()
    updated=alice.request('GET',f"/api/v1/conversations/{identifier}/updates?after=1&etag={page['etag']}").json()
    assert not updated['reset'] and not updated['unchanged']
    assert updated['etag']!=page['etag']
    assert [turn['task_id'] for turn in updated['turns']]==[first['task_id']]


@pytest.mark.parametrize(('decision','expected_status'), [('APPROVED','APPROVED'),('REJECTED','CANCELED')])
def test_approval_receipt_updates_chat_without_worker(system,alice,decision,expected_status):
    from homeai.crypto import canonical,digest
    from homeai.db import Approval,Invocation,now
    app=system[0].state
    identifier=new_chat(alice)
    first=send(alice,identifier,'请确认后再发送邮件').json()
    invocation_id=str(uuid.uuid4());approval_id=str(uuid.uuid4())
    arguments={'to':'recipient@example.test','subject':'审批回执验证','text':'不实际投递'}
    arguments_hash=digest(canonical(arguments))
    # 只建立待审批状态；提交决定后不运行 Worker，也不触发邮件或模型。
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        db.get(Task,first['task_id']).status='AWAITING_APPROVAL'
        db.add(Invocation(id=invocation_id,household_id='h1',owner_id=alice.user_id,task_id=first['task_id'],step=0,
            capability='mail.send@v1',arguments_hash=arguments_hash,
            arguments=app.vault.seal(arguments,alice.user_id+':invocation:'+invocation_id)))
        db.add(Approval(id=approval_id,household_id='h1',owner_id=alice.user_id,invocation_id=invocation_id,
            arguments_hash=arguments_hash,expires_at=now()+300))
        db.commit()
    page=alice.request('GET','/api/v1/conversations/'+identifier).json()
    assert page['turns'][0]['status']=='AWAITING_APPROVAL'
    assert [item['id'] for item in page['turns'][0]['approvals']]==[approval_id]
    url=f"/api/v1/conversations/{identifier}/updates?after={first['sequence']}&etag={page['etag']}"
    assert alice.request('GET',url).json()['unchanged']

    receipt=alice.request('POST','/api/v1/approvals/'+approval_id,{'decision':decision})
    assert receipt.status_code==200,receipt.text
    assert receipt.json()=={'status':expected_status}
    updated=alice.request('GET',url).json()
    assert not updated['reset'] and not updated['unchanged']
    assert updated['etag']!=page['etag']
    assert len(updated['turns'])==1
    assert updated['turns'][0]['task_id']==first['task_id']
    assert updated['turns'][0]['status']==expected_status
    assert updated['turns'][0]['approvals']==[]
    assert alice.request('GET','/api/v1/approvals').json()==[]
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        assert db.get(Approval,approval_id).decision==decision
        assert db.get(Task,first['task_id']).cancel_requested==(decision=='REJECTED')
        assert db.get(Invocation,invocation_id).status=='PENDING'
        assert db.get(Invocation,invocation_id).result is None
    # 原有重复审批语义仍为409，拒绝的重复提交也不能制造新的会话事件。
    assert alice.request('POST','/api/v1/approvals/'+approval_id,{'decision':decision}).status_code==409
    latest=f"/api/v1/conversations/{identifier}/updates?after={first['sequence']}&etag={updated['etag']}"
    assert alice.request('GET',latest).json()=={'turns':[],'etag':updated['etag'],'reset':False,'unchanged':True}


def test_reminder_intents_append_to_default_chat(system,alice):
    identifier=alice.request('POST','/api/v1/conversations/default').json()['id']
    body={'idempotency_key':str(uuid.uuid4()),'title':'持续会话提醒'}
    first=alice.request('POST','/api/v1/input/reminder',body)
    assert first.status_code==202,first.text
    assert first.json()['conversation_id']==identifier
    assert alice.request('POST','/api/v1/input/reminder',body).json()==first.json()
    assert len(alice.request('GET','/api/v1/conversations/'+identifier).json()['turns'])==1


def test_progress_query_does_not_redispatch_running_task(system,alice):
    identifier=new_chat(alice)
    task=send(alice,identifier,'请搜索公开资料').json()['task_id']
    with system[2]() as db:
        scope(db,alice.user_id,'h1');db.get(Task,task).status='EXECUTING';db.commit()
    progress=send(alice,identifier,'任务进度').json()
    assert progress['status']=='SUCCEEDED'
    assert '执行中' in progress['assistant_text']
    assert progress['interaction_kind']=='chat'
    with system[2]() as db:
        scope(db,alice.user_id,'h1');assert db.get(Task,task).status=='EXECUTING'


def test_explicit_personal_memory_is_saved_without_second_confirmation(system,alice):
    identifier=new_chat(alice)
    turn=send(alice,identifier,'请记住我的工作是设计师').json()
    assert turn['status']=='SUCCEEDED' and '记住了' in turn['assistant_text']
    candidates=alice.request('GET','/api/v1/memory/candidates').json()
    assert candidates==[]
    entries=alice.request('GET','/api/v1/memory/entries').json()
    assert len(entries)==1 and entries[0]['kind']=='memory.fact'
    assert not any(row['kind'].startswith('memory.') for row in alice.request('GET','/api/v1/data').json()['records'])
