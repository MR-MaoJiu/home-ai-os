"""隔离临时数据库中的安全重试状态机；不调用真实外部副作用。"""
import uuid
import pytest
from sqlalchemy import select, func
from homeai.crypto import canonical, digest
from homeai.db import Task, Invocation, Approval, Record, Device, now
from homeai.runtime import run_task
from conftest import SignedClient
from homeai.image_generation import ImageUsage


def create(alice, steps=None):
    body={'idempotency_key':str(uuid.uuid4()),'steps':steps or [{'capability':'calendar.search@v1','arguments':{'query':'会议'}}]}
    result=alice.request('POST','/api/v1/tasks',body)
    assert result.status_code==202,result.text
    return result.json()['id']


def fail(system,alice,tid,status='FAILED',capability='calendar.search@v1',invocation_status='FAILED',step=0,approval=None):
    with system[2]() as db:
        task=db.get(Task,tid);task.status=status;task.error='隔离故障'
        body=system[0].state.vault.open(task.request,alice.user_id+':task:'+tid)
        args=(body.get('steps') or [{}])[step].get('arguments',{})
        row=Invocation(id=str(uuid.uuid4()),owner_id=alice.user_id,household_id='h1',task_id=tid,step=step,
            capability=capability,arguments_hash=digest(canonical(args)),arguments='pending',status=invocation_status)
        row.arguments=system[0].state.vault.seal(args,alice.user_id+':invocation:'+row.id)
        db.add(row)
        if approval:
            value=Approval(id=str(uuid.uuid4()),owner_id=alice.user_id,household_id='h1',invocation_id=row.id,
                arguments_hash=row.arguments_hash,decision=approval,expires_at=now()-1)
            db.add(value)
        db.commit()
        return row.id


def retry(alice,tid,key='manual-retry-one'):
    return alice.request('POST',f'/api/v1/tasks/{tid}/retry',{'idempotency_key':key})


@pytest.mark.asyncio
async def test_retry_after_deadline_preserves_real_completed_effect(system,alice):
    tid=create(alice,[{'capability':'reminder.create@v1','arguments':{'title':'已完成步骤不可重放'}},
                      {'capability':'calendar.search@v1','arguments':{'query':'会议'}}])
    await run_task(system[0].state,tid,alice.user_id)
    first=alice.request('GET',f'/api/v1/tasks/{tid}/steps').json()[0]
    with system[2]() as db:
        db.get(Task,tid).deadline=now()-1;db.commit()
    await run_task(system[0].state,tid,alice.user_id)
    assert alice.request('GET',f'/api/v1/tasks/{tid}').json()['status']=='FAILED'
    preview=alice.request('GET',f'/api/v1/tasks/{tid}/retry').json()
    assert preview['can_retry'] and preview['completed_steps']==1 and preview['retry_step']==1
    assert retry(alice,tid).json()=={'id':tid,'status':'RECEIVED','replayed':False}
    await run_task(system[0].state,tid,alice.user_id)
    assert alice.request('GET',f'/api/v1/tasks/{tid}').json()['status']=='SUCCEEDED'
    steps=alice.request('GET',f'/api/v1/tasks/{tid}/steps').json()
    assert steps[0]['id']==first['id'] and steps[0]['result']==first['result']
    with system[2]() as db:
        assert db.scalar(select(func.count()).select_from(Record).where(Record.owner_id==alice.user_id,Record.kind=='reminder.item'))==1


def test_read_retry_is_idempotent_and_preserves_budget_context(system,alice):
    tid=create(alice);fail(system,alice,tid)
    with system[2]() as db:
        task=db.get(Task,tid)
        body=system[0].state.vault.open(task.request,alice.user_id+':task:'+tid)
        body.update(_attempts={'0':2,'9':4},_model_token_charge=777,_model_rounds=3,_client_context={'sampled_at':'original'},_privacy_review={'status':'approved','expires_at':now()+999})
        task.request=system[0].state.vault.seal(body,alice.user_id+':task:'+tid);db.commit()
    assert retry(alice,tid).status_code==200
    with system[2]() as db:
        task=db.get(Task,tid);deadline=task.deadline
        body=system[0].state.vault.open(task.request,alice.user_id+':task:'+tid)
        assert body['_attempts']=={'9':4} and body['_model_token_charge']==777 and body['_model_rounds']==3
        assert body['_client_context']=={'sampled_at':'original'} and '_privacy_review' not in body
        assert db.scalar(select(Invocation).where(Invocation.task_id==tid)).status=='PENDING'
    assert retry(alice,tid).json()['replayed'] is True
    with system[2]() as db:
        task=db.get(Task,tid);assert task.deadline==deadline
        body=system[0].state.vault.open(task.request,alice.user_id+':task:'+tid)
        assert len(body['_manual_retries'])==1
    assert retry(alice,tid,'different-retry-key').status_code==409


@pytest.mark.parametrize('status,invocation_status',[('NEEDS_RECONCILIATION','NEEDS_RECONCILIATION'),('FAILED','FAILED'),('FAILED','EXECUTING')])
def test_uncertain_or_failed_effect_never_replays(system,alice,status,invocation_status):
    tid=create(alice,[{'capability':'mail.send@v1','arguments':{'to':'test@example.com','subject':'不得发送','text':'隔离测试'}}])
    fail(system,alice,tid,status=status,capability='mail.send@v1',invocation_status=invocation_status)
    assert not alice.request('GET',f'/api/v1/tasks/{tid}/retry').json()['can_retry']
    assert retry(alice,tid).status_code==409
    with system[2]() as db:assert db.get(Task,tid).status==status


def test_pending_risky_effect_requires_new_approval_identity(system,alice):
    tid=create(alice,[{'capability':'mail.send@v1','arguments':{'to':'test@example.com','subject':'不执行发送','text':'审批测试'}}])
    invocation=fail(system,alice,tid,capability='mail.send@v1',invocation_status='PENDING',approval='APPROVED')
    with system[2]() as db:old=db.scalar(select(Approval).where(Approval.invocation_id==invocation)).id
    assert retry(alice,tid).json()['status']=='AWAITING_APPROVAL'
    pending=alice.request('GET','/api/v1/approvals').json()
    assert len(pending)==1 and pending[0]['id']!=old
    assert alice.request('POST','/api/v1/approvals/'+old,{'decision':'APPROVED'}).status_code==404
    with system[2]() as db:
        row=db.scalar(select(Invocation).where(Invocation.task_id==tid));assert row.status=='PENDING'


@pytest.mark.parametrize('reason',['cancel','approval','device','source','budget'])
def test_retry_rechecks_authorization_and_never_refills_budget(system,alice,reason):
    tid=create(alice)
    fail(system,alice,tid,approval='REJECTED' if reason=='approval' else None)
    with system[2]() as db:
        task=db.get(Task,tid)
        payload=system[0].state.vault.open(task.request,alice.user_id+':task:'+tid)
        if reason=='cancel':task.cancel_requested=True
        elif reason=='device':
            old=Device(id=str(uuid.uuid4()),user_id=alice.user_id,public_key='fixture',name='旧设备',revoked=True)
            db.add(old);payload['device_id']=old.id
        elif reason=='source':payload['_record_dependencies']={str(uuid.uuid4()):1}
        elif reason=='budget':payload.update(_agent=True,_model_rounds=payload['max_steps']+2)
        task.request=system[0].state.vault.seal(payload,alice.user_id+':task:'+tid);db.commit()
    preview=alice.request('GET',f'/api/v1/tasks/{tid}/retry').json()
    assert not preview['can_retry'] and preview['reason']
    assert retry(alice,tid).status_code==409


def test_retry_does_not_allow_other_member_to_probe_or_queue(system,alice):
    tid=create(alice);fail(system,alice,tid)
    bob=SignedClient(system[1],system[2],household='h1')
    assert bob.request('GET',f'/api/v1/tasks/{tid}/retry').status_code==404
    assert retry(bob,tid).status_code==404


@pytest.mark.parametrize('evidence',[None,'usage','dispatch','abort'])
def test_image_retry_requires_absence_of_both_dispatch_evidence(system,alice,evidence):
    tid=create(alice,[{'capability':'image.generate@v1','arguments':{'prompt':'公开图案'}}])
    identifier=fail(system,alice,tid,capability='image.generate@v1')
    with system[2]() as db:
        task=db.get(Task,tid)
        payload=system[0].state.vault.open(task.request,alice.user_id+':task:'+tid)
        if evidence=='usage':db.add(ImageUsage(owner_id=alice.user_id,household_id='h1',task_id=tid,call_key=identifier,day='2026-09-30',charged=1,status='RESERVED'))
        elif evidence=='dispatch':payload['_image_dispatched']=identifier
        elif evidence=='abort':db.get(Invocation,identifier).result=system[0].state.vault.seal({'confirmation_source':'user_reconciliation'},alice.user_id+':invocation-result:'+identifier)
        task.request=system[0].state.vault.seal(payload,alice.user_id+':task:'+tid);db.commit()
    preview=alice.request('GET',f'/api/v1/tasks/{tid}/retry').json()
    assert preview['can_retry'] is (evidence is None)
    assert retry(alice,tid).status_code==(200 if evidence is None else 409)


def test_failed_search_retry_resets_only_failed_tail_not_consumed_budget(system,alice):
    steps=[{'capability':'web.search@v1','arguments':{'query':'公开资料一'}},{'capability':'web.search@v1','arguments':{'query':'公开资料二'}}]
    tid=create(alice,steps)
    fail(system,alice,tid,capability='web.search@v1',invocation_status='UNAVAILABLE',step=0)
    fail(system,alice,tid,capability='web.search@v1',invocation_status='UNAVAILABLE',step=1)
    with system[2]() as db:
        task=db.get(Task,tid)
        payload=system[0].state.vault.open(task.request,alice.user_id+':task:'+tid)
        payload.update(_agent=True,max_steps=2,_model_rounds=3,_model_token_charge=777,
                       _answer_from_existing=True,_planner_feedback='旧的失败收尾',_final_round_used=True,_duplicate_reads=2,
                       _attempts={'0':2,'1':2},_read_queries={'query-signature':0})
        task.request=system[0].state.vault.seal(payload,alice.user_id+':task:'+tid);db.commit()
    assert retry(alice,tid).status_code==200
    with system[2]() as db:
        task=db.get(Task,tid);payload=system[0].state.vault.open(task.request,alice.user_id+':task:'+tid)
        assert payload['_model_rounds']==3 and payload['_model_token_charge']==777 and payload['max_steps']==2
        assert payload['_read_queries']=={'query-signature':0} and payload['_attempts']=={}
        assert all(key not in payload for key in ('_answer_from_existing','_planner_feedback','_final_round_used','_duplicate_reads'))
        assert [row.status for row in db.scalars(select(Invocation).where(Invocation.task_id==tid))]==['PENDING','PENDING']


def test_cloud_model_reserved_call_cannot_offer_false_retry(system,alice):
    from homeai.model_routing import ModelUsage
    tid=create(alice,[{'capability':'model.generate@v1','arguments':{'messages':[{'role':'user','content':'公开资料'}]}}])
    identifier=fail(system,alice,tid,capability='model.generate@v1')
    with system[2]() as db:
        db.add(ModelUsage(owner_id=alice.user_id,household_id='h1',task_id=tid,call_key=identifier,
                          day='2026-09-30',reserved=123,charged=123,status='UNKNOWN'))
        db.commit()
    preview=alice.request('GET',f'/api/v1/tasks/{tid}/retry').json()
    assert not preview['can_retry'] and preview['code']=='model_dispatch_reserved'
    assert retry(alice,tid).status_code==409
    with system[2]() as db:
        assert db.get(Task,tid).status=='FAILED'
        usage=db.scalar(select(ModelUsage).where(ModelUsage.call_key==identifier))
        assert usage.charged==123 and usage.status=='UNKNOWN'
