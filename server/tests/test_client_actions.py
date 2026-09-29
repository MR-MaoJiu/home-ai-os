"""临时数据库验证：客户端请求、逐任务授权与安全恢复，不实际投递推送。"""
import uuid
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from conftest import SignedClient
from test_security_data import put,record
from homeai.crypto import canonical,digest
from homeai.db import Task,Invocation,Approval,Notification,Record,scope,now
from homeai.security import Actor
from homeai.client_actions import (ClientAction,TaskDataGrant,create_request,cloud_consent_status,execute,
    read_authorized_record,check_task_grants,expire_requests,validate_mentions,task_actions)


def actor(client):return Actor(client.user_id,'h1',client.device_id,'adult')


def new_task(system,client,mentions=()):
    response=client.request('POST','/api/v1/tasks',{'idempotency_key':str(uuid.uuid4()),'message':'请求数据的私人聊天'})
    assert response.status_code==202,response.text
    tid=response.json()['id']
    with system[2]() as db:
        scope(db,client.user_id,'h1');task=db.get(Task,tid)
        payload=system[0].state.vault.open(task.request,client.user_id+':task:'+tid)
        payload.update(_task_id=tid,_mentions=[{'member_id':item.user_id} for item in mentions])
        task.request=system[0].state.vault.seal(payload,client.user_id+':task:'+tid);db.commit()
    return tid


def request_action(system,client,tid,kind='choose_photos',params=None,target=None):
    with system[2]() as db:
        scope(db,client.user_id,'h1');task=db.get(Task,tid)
        result=create_request(system[0].state,db,actor(client),task,kind,'请提供所需资料',params or {},target_member_id=target)
        db.commit();return result['client_action_id']


def response(client,aid,**values):
    return client.request('POST','/api/v1/client-actions/'+aid+'/respond',{'idempotency_key':str(uuid.uuid4()),**values})


def test_member_request_scope_receipt_grant_and_revoke(system,alice):
    bob=SignedClient(system[1],system[2]);other=SignedClient(system[1],system[2]);outsider=SignedClient(system[1],system[2],household='h2')
    tid=new_task(system,alice,[bob]);aid=request_action(system,alice,tid,'data.share',{},bob.user_id)
    assert alice.request('GET','/api/v1/tasks/'+tid).json()['status']=='WAITING_CLIENT'
    item=bob.request('GET','/api/v1/client-actions/'+aid).json()
    assert item['can_respond'] and item['task_id'] is None and item['conversation_id'] is None
    assert other.request('GET','/api/v1/client-actions/'+aid).status_code==404
    assert outsider.request('GET','/api/v1/client-actions/'+aid).status_code==404
    assert response(alice,aid).status_code==404
    rid=put(bob,record(payload={'title':'明确提供的一条私人资料'}))
    body={'idempotency_key':str(uuid.uuid4()),'record_ids':[rid]}
    accepted=bob.request('POST','/api/v1/client-actions/'+aid+'/respond',body)
    assert accepted.status_code==200,accepted.text
    assert bob.request('POST','/api/v1/client-actions/'+aid+'/respond',body).json()==accepted.json()
    assert response(bob,aid,record_ids=[rid]).status_code==409
    assert alice.request('GET','/api/v1/tasks/'+tid).json()['status']=='RECEIVED'
    assert alice.request('GET','/api/v1/data/'+rid).status_code==404
    assert alice.request('GET','/api/v1/tasks/'+tid+'/data/'+rid).status_code==200
    second=new_task(system,alice)
    assert alice.request('GET','/api/v1/tasks/'+second+'/data/'+rid).status_code==404
    assert bob.request('POST','/api/v1/client-actions/'+aid+'/revoke').status_code==200
    assert alice.request('GET','/api/v1/tasks/'+tid+'/data/'+rid).status_code==403
    assert bob.request('GET','/api/v1/data/'+rid).json()['visibility']=='personal'


def test_cross_member_needs_mention_and_never_accepts_memory_or_other_owners(system,alice):
    bob=SignedClient(system[1],system[2]);tid=new_task(system,alice)
    with pytest.raises(HTTPException) as error:request_action(system,alice,tid,'data.share',{},bob.user_id)
    assert error.value.status_code==403
    with system[2]() as db:
        with pytest.raises(HTTPException):validate_mentions(db,actor(alice),[{'member_id':bob.user_id,'role':'administrator'}])
    tid=new_task(system,alice,[bob]);aid=request_action(system,alice,tid,'data.share',{},bob.user_id)
    private=put(alice,record(source_id='alice'))
    memory=put(bob,record(kind='memory.fact'))
    secret=record(source_id='secret');secret['sensitivity']='SECRET';secret=put(bob,secret)
    for rid in (private,memory,secret):assert response(bob,aid,record_ids=[rid]).status_code in {403,404}
    assert bob.request('GET','/api/v1/client-actions/'+aid).json()['status']=='PENDING'


def test_wait_expiry_and_cancellation_prevent_late_response(system,alice):
    tid=new_task(system,alice);aid=request_action(system,alice,tid,'provide_text',{})
    with system[2]() as db:
        scope(db,alice.user_id,'h1');row=db.get(ClientAction,aid);row.expires_at=now()-1;db.commit()
    expire_requests(system[0].state)
    assert alice.request('GET','/api/v1/client-actions/'+aid).json()['status']=='EXPIRED'
    assert response(alice,aid,text='太晚的回答').status_code==409
    tid=new_task(system,alice);aid=request_action(system,alice,tid,'provide_text',{})
    assert alice.request('POST','/api/v1/tasks/'+tid+'/cancel').status_code==200
    assert response(alice,aid,text='已取消后回答').status_code==409
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        assert not db.scalar(select(Record).where(Record.source=='client_action',Record.source_id==aid))


def test_decline_is_idempotent_and_cloud_consent_bound_to_scope(system,alice):
    tid=new_task(system,alice)
    params={'scope_hash':'a'*64,'record_versions':{},'provider_id':'cloud-test'}
    aid=request_action(system,alice,tid,'cloud.disclose',params)
    body={'idempotency_key':str(uuid.uuid4()),'reason':'不允许'}
    denied=alice.request('POST','/api/v1/client-actions/'+aid+'/deny',body)
    assert denied.status_code==200
    assert alice.request('POST','/api/v1/client-actions/'+aid+'/deny',body).json()==denied.json()
    with system[2]() as db:
        assert cloud_consent_status(system[0].state,db,actor(alice),tid,'a'*64,{})=='denied'
        assert cloud_consent_status(system[0].state,db,actor(alice),tid,'b'*64,{})=='missing'
    tid=new_task(system,alice);aid=request_action(system,alice,tid,'cloud.disclose',params)
    assert response(alice,aid).status_code==200
    with system[2]() as db:
        assert cloud_consent_status(system[0].state,db,actor(alice),tid,'a'*64,{})=='approved'


def test_data_owner_must_approve_cross_member_cloud_scope(system,alice):
    bob=SignedClient(system[1],system[2]);tid=new_task(system,alice,[bob])
    rid=put(bob,record(payload={'title':'B拥有的资料'}))
    data_action=request_action(system,alice,tid,'data.share',{'record_ids':[rid]},bob.user_id)
    assert response(bob,data_action,record_ids=[rid]).status_code==200
    params={'scope_hash':'c'*64,'record_versions':{rid:1},'provider_id':'cloud-test'}
    with pytest.raises(HTTPException):request_action(system,alice,tid,'cloud.disclose',params)
    aid=request_action(system,alice,tid,'cloud.disclose',params,bob.user_id)
    view=bob.request('GET','/api/v1/client-actions/'+aid).json()
    assert view['purpose']=='允许本次任务向指定云模型披露所选资料的脱敏内容'
    assert '私人聊天' not in str(view)
    assert response(alice,aid).status_code==404
    assert response(bob,aid).status_code==200
    with system[2]() as db:
        assert cloud_consent_status(system[0].state,db,actor(alice),tid,'c'*64,{rid:1},owner_id=bob.user_id)=='approved'
        assert cloud_consent_status(system[0].state,db,actor(alice),tid,'c'*64,{rid:1})=='missing'
    bob.request('POST','/api/v1/client-actions/'+aid+'/revoke')
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,tid)
        payload=system[0].state.vault.open(task.request,alice.user_id+':task:'+tid)
        with pytest.raises(HTTPException):check_task_grants(db,actor(alice),payload)


def test_location_scope_and_health_window_are_checked(system,alice):
    tid=new_task(system,alice);aid=request_action(system,alice,tid,'capture_location',{'precision':'coarse'})
    loc=put(alice,record(kind='location.point',payload={'latitude':1,'longitude':1,'precision':'precise','timestamp':'2000-01-01T00:00:00Z'}))
    assert response(alice,aid,record_ids=[loc]).status_code==422
    assert response(alice,aid,text='绕过数据类型').status_code==422


def test_targeted_notification_requires_approval_and_is_idempotent(system,alice):
    bob=SignedClient(system[1],system[2]);tid=new_task(system,alice,[bob])
    args={'target_member_id':bob.user_id,'summary':'请看家庭通知'}
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,tid)
        invocation=Invocation(id=str(uuid.uuid4()),owner_id=alice.user_id,household_id='h1',task_id=tid,step=0,capability='member.notify@v1',arguments='encrypted-test-not-read',arguments_hash=digest(canonical(args)))
        db.add(invocation);db.flush()
        with pytest.raises(HTTPException):execute(system[0].state,db,actor(alice),task,invocation,'member.notify@v1',args)
        db.add(Approval(owner_id=alice.user_id,household_id='h1',invocation_id=invocation.id,arguments_hash=invocation.arguments_hash,decision='APPROVED',expires_at=now()+300));db.flush()
        result=execute(system[0].state,db,actor(alice),task,invocation,'member.notify@v1',args)
        assert execute(system[0].state,db,actor(alice),task,invocation,'member.notify@v1',args)==result
        db.commit()
    item=bob.request('GET','/api/v1/client-actions').json()['items'][0]
    assert item['kind']=='member.notify' and item['parameters']['summary']=='请看家庭通知' and not item['can_respond']
    assert bob.request('GET','/api/v1/client-actions?notification_id='+item['notification_id']).json()['items'][0]['id']==item['id']
    assert len(bob.request('GET','/api/v1/notifications').json()['items'])==1


def test_option_response_must_match_server_enum_and_cancellation_clears_pending(system,alice):
    from homeai.client_actions import cancel_task_actions
    tid=new_task(system,alice)
    aid=request_action(system,alice,tid,'choose_option',{'prompt':'选择执行方式','options':[{'id':'local','label':'本地'},{'id':'cloud','label':'云端'}]})
    assert response(alice,aid,text='arbitrary-other').status_code==422
    assert response(alice,aid,text='local').status_code==200
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        stored=db.scalar(select(Record).where(Record.source_id==aid,Record.source=='client_action'))
        assert system[0].state.vault.open(stored.payload,alice.user_id+':record:'+stored.id)['option_id']=='local'
    second=new_task(system,alice);pending=request_action(system,alice,second,'provide_text',{})
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,second)
        cancel_task_actions(system[0].state,db,actor(alice),task);db.commit()
    assert alice.request('GET','/api/v1/client-actions/'+pending).json()['status']=='CANCELED'
    assert response(alice,pending,text='不再接收').status_code==409


def _approved_notice(system,alice,tid,target,summary,step):
    args={'target_member_id':target,'summary':summary}
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,tid)
        invocation=Invocation(id=str(uuid.uuid4()),owner_id=alice.user_id,household_id='h1',task_id=tid,step=step,capability='member.notify@v1',arguments='encrypted-test-only',arguments_hash=digest(canonical(args)))
        db.add(invocation);db.flush()
        db.add(Approval(owner_id=alice.user_id,household_id='h1',invocation_id=invocation.id,arguments_hash=invocation.arguments_hash,decision='APPROVED',expires_at=now()+300));db.flush()
        result=execute(system[0].state,db,actor(alice),task,invocation,'member.notify@v1',args)
        db.commit();return result['client_action_id']


def test_private_grant_cannot_be_forwarded_and_revocation_hides_sent_summary(system,alice):
    bob=SignedClient(system[1],system[2]);charlie=SignedClient(system[1],system[2]);tid=new_task(system,alice,[bob,charlie])
    rid=put(bob,record(payload={'title':'B的私人事实'}))
    aid=request_action(system,alice,tid,'data.share',{},bob.user_id)
    assert response(bob,aid,record_ids=[rid]).status_code==200
    with pytest.raises(HTTPException) as error:_approved_notice(system,alice,tid,charlie.user_id,'B的私人事实',0)
    assert error.value.status_code==403
    notice_id=_approved_notice(system,alice,tid,bob.user_id,'B的私人事实',1)
    assert bob.request('GET','/api/v1/client-actions/'+notice_id).json()['parameters']['summary']=='B的私人事实'
    before=len(bob.request('GET','/api/v1/notifications').json()['items'])
    assert bob.request('POST','/api/v1/client-actions/'+aid+'/revoke').status_code==200
    hidden=bob.request('GET','/api/v1/client-actions/'+notice_id).json()
    assert hidden['status']=='REVOKED' and 'B的私人事实' not in str(hidden)
    notices=bob.request('GET','/api/v1/notifications').json()['items']
    assert len(notices)>before
    assert bob.request('GET','/api/v1/client-actions?notification_id='+notices[0]['id']).json()['items'][0]['id']==notice_id


def test_cross_member_purpose_does_not_copy_private_chat(system,alice):
    bob=SignedClient(system[1],system[2]);tid=new_task(system,alice,[bob])
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,tid)
        action=create_request(system[0].state,db,actor(alice),task,'data.share','A的整段私人聊天不应披露给B',{},target_member_id=bob.user_id)
        db.commit()
    view=bob.request('GET','/api/v1/client-actions/'+action['client_action_id']).json()
    assert 'A的整段' not in str(view)


def test_health_snapshot_types_limits_and_no_data_are_explicit(system,alice):
    from datetime import datetime,timezone,timedelta
    from homeai.client_actions import validate_health_record
    end=datetime.now(timezone.utc)-timedelta(minutes=1);start=end-timedelta(days=1)
    params={'types':['sleep','steps','heart_rate','body_mass'],'start_at':start.isoformat(),'end_at':end.isoformat()}
    tid=new_task(system,alice);aid=request_action(system,alice,tid,'read_health',params)
    snapshot={'types':params['types'],'start_at':params['start_at'],'end_at':params['end_at'],'samples':[],'status':'no_data'}
    rid=put(alice,record(kind='health.snapshot',payload=snapshot))
    assert response(alice,aid,record_ids=[rid]).status_code==200
    sample={'type':'steps','sample_id':'sample1','start_at':params['start_at'],'end_at':params['end_at'],'value':1234,'unit':'count'}
    row=Record(id='test',kind='health.snapshot')
    validate_health_record(row,{**snapshot,'samples':[sample],'status':'available'},params)
    for changed in [{**sample,'value':float('inf')},{**sample,'value':True},{**sample,'unit':'kg'},{**sample,'type':'blood_pressure'},{**sample,'start_at':'1900-01-01T00:00:00Z'}]:
        with pytest.raises(HTTPException):validate_health_record(row,{**snapshot,'samples':[changed],'status':'available'},params)
    with pytest.raises(HTTPException):validate_health_record(row,{**snapshot,'samples':[sample]*1001,'status':'available'},params)


def test_task_scoped_media_wrappers_and_scope_revalidation(system,alice):
    from test_media import upload,png
    from homeai.client_actions import authorized_source_scope
    bob=SignedClient(system[1],system[2]);tid=new_task(system,alice,[bob])
    completed=upload(bob,png(),'grant.png','image')
    assert completed.status_code==200,completed.text
    rid=completed.json()['asset']['record_id']
    aid=request_action(system,alice,tid,'choose_photos',{},bob.user_id)
    assert response(bob,aid,record_ids=[rid]).status_code==200
    assert alice.request('GET','/api/v1/assets/'+rid).status_code==404
    prefix='/api/v1/tasks/'+tid+'/assets/'+rid
    assert alice.request('GET',prefix).json()['kind']=='image'
    downloaded=alice.request('GET',prefix+'/content')
    assert downloaded.status_code==200 and downloaded.content==png()
    assert alice.request('GET',prefix+'/thumbnail').content.startswith(b'\xff\xd8')
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,tid);payload=system[0].state.vault.open(task.request,alice.user_id+':task:'+tid)
        with pytest.raises(HTTPException):
            with authorized_source_scope(db,actor(alice),rid,payload) as source:
                assert source.user_id==bob.user_id
                # 授权在耗时读取途中改变，退出source scope时必须拒绝旧数据。
                grant=db.scalar(select(TaskDataGrant).where(TaskDataGrant.action_id==aid))
                grant.revoked=True;db.flush()
        assert db.info['user_id']==alice.user_id
        db.rollback()
    assert bob.request('POST','/api/v1/client-actions/'+aid+'/revoke').status_code==200
    for suffix in ('','/content','/thumbnail'):
        assert alice.request('GET',prefix+suffix).status_code==403


def test_restore_invalidates_private_grants_cloud_consents_and_pending_actions(system,alice):
    from homeai.backup import invalidate_restored_authorizations
    bob=SignedClient(system[1],system[2]);tid=new_task(system,alice,[bob]);rid=put(bob)
    aid=request_action(system,alice,tid,'data.share',{},bob.user_id)
    assert response(bob,aid,record_ids=[rid]).status_code==200
    cloud=request_action(system,alice,tid,'cloud.disclose',{'scope_hash':'d'*64,'record_versions':{rid:1},'provider_id':'test-cloud'},bob.user_id)
    assert response(bob,cloud).status_code==200
    pending_task=new_task(system,alice);pending=request_action(system,alice,pending_task,'provide_text',{})
    result=invalidate_restored_authorizations(system[2],system[0].state.vault)
    assert result['revoked_grants']==1 and result['revoked_cloud_consents']==1 and result['canceled_requests']==1
    assert alice.request('GET','/api/v1/tasks/'+tid+'/data/'+rid).status_code==403
    assert alice.request('GET','/api/v1/client-actions/'+pending).json()['status']=='CANCELED'
    assert response(alice,pending,text='旧手机迟到的响应').status_code==409
