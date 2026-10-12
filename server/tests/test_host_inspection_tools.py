"""性能统计自动执行，截屏仍经过任务审批；截图测试仅使用公开像素。"""
import json
import uuid
import pytest
from homeai.runtime import run_task
from homeai.shell_configuration import ShellPolicy
from homeai.db import Task, Invocation, Approval, now, uid
from test_media import png


def submit(user,targets):
    value=user.request('POST','/api/v1/tasks',{'idempotency_key':str(uuid.uuid4()),
        'capability':'host.inspect@v1','arguments':{'targets':targets,'purpose':'检查本次服务状态'}})
    assert value.status_code==202,value.text
    return value.json()['id']


async def approve(system,user,tid):
    await run_task(system[0].state,tid,user.user_id)
    assert user.request('GET','/api/v1/tasks/'+tid).json()['status']=='AWAITING_APPROVAL'
    approval=next(row for row in user.request('GET','/api/v1/approvals').json() if row['task_id']==tid)
    context=approval['arguments']['approval_context']
    assert context['command'] and context['effects']['summary'] and context['execution_location']
    assert user.request('POST','/api/v1/approvals/'+approval['id'],{'decision':'APPROVED'}).status_code==200
    return approval


@pytest.mark.parametrize('targets',[['cpu'],['memory'],['cpu','memory']])
async def test_metrics_continue_without_client_approval(system,alice,monkeypatch,targets):
    from homeai import host_inspection
    calls=[]
    async def collect(targets):
        calls.append(targets);return {target:{'sample':'public-test-value'} for target in targets}
    monkeypatch.setattr(host_inspection,'collect_metrics',collect)
    tid=submit(alice,targets)
    await run_task(system[0].state,tid,alice.user_id)
    result=alice.request('GET','/api/v1/tasks/'+tid).json()
    assert result['status']=='SUCCEEDED' and set(result['result']['observations'])==set(targets)
    assert calls==[sorted(targets)]
    assert not any(row['task_id']==tid for row in alice.request('GET','/api/v1/approvals').json())
    await run_task(system[0].state,tid,alice.user_id)
    assert calls==[sorted(targets)]


async def test_screenshot_requires_local_member_grant_then_approval(system,alice,monkeypatch):
    from homeai import host_inspection
    calls=[]
    async def screen():calls.append('captured');return png()
    monkeypatch.setattr(host_inspection,'capture_screen',screen)
    monkeypatch.setattr('homeai.host_inspection_tools.platform.system',lambda:'Darwin')
    blocked=submit(alice,['screenshot'])
    await run_task(system[0].state,blocked,alice.user_id)
    assert alice.request('GET','/api/v1/tasks/'+blocked).json()['status']=='FAILED'
    assert calls==[]
    path=system[0].state.settings.state_dir/'shell-policy.json'
    path.write_text(ShellPolicy(host_enabled=True,host_members=[alice.user_id]).model_dump_json());path.chmod(0o600)
    tid=submit(alice,['screenshot']);await approve(system,alice,tid)
    assert calls==[]
    await run_task(system[0].state,tid,alice.user_id)
    response=alice.request('GET','/api/v1/tasks/'+tid).json()
    assert response['status']=='SUCCEEDED',response
    part=response['result']['parts'][0]
    assert part['provenance']['origin']=='server_capture'
    assert alice.request('GET','/api/v1/assets/'+part['record_id']+'/content').content==png()
    assert len(calls)==1


async def test_denied_screenshot_does_not_collect_even_requested_metrics(system,alice,monkeypatch):
    async def fail(*args):raise AssertionError('拒绝后不能采集')
    monkeypatch.setattr('homeai.host_inspection.collect_metrics',fail)
    monkeypatch.setattr('homeai.host_inspection.capture_screen',fail)
    monkeypatch.setattr('homeai.host_inspection_tools.platform.system',lambda:'Darwin')
    path=system[0].state.settings.state_dir/'shell-policy.json'
    path.write_text(ShellPolicy(host_enabled=True,host_members=[alice.user_id]).model_dump_json());path.chmod(0o600)
    tid=submit(alice,['cpu','memory','screenshot'])
    await run_task(system[0].state,tid,alice.user_id)
    entry=next(x for x in alice.request('GET','/api/v1/approvals').json() if x['task_id']==tid)
    assert alice.request('POST','/api/v1/approvals/'+entry['id'],{'decision':'REJECTED'}).status_code==200
    await run_task(system[0].state,tid,alice.user_id)
    assert alice.request('GET','/api/v1/tasks/'+tid).json()['status']=='CANCELED'


def legacy_pending_inspection(system,alice,*,targets=None,decision='PENDING',expired_task=False,expired_approval=False,canceled=False,invocation_status='PENDING'):
    from homeai.crypto import canonical,digest
    from homeai.host_inspection_tools import prepare
    from homeai.security import Actor
    targets=targets or ['cpu']
    tid=submit(alice,targets)
    with system[2]() as db:
        task=db.get(Task,tid)
        actor=Actor(alice.user_id,'h1',alice.device_id,'infrastructure_owner')
        arguments={'targets':targets,'purpose':'检查本次服务状态'}
        if 'screenshot' not in targets:
            body=system[0].state.vault.open(task.request,alice.user_id+':task:'+tid)
            arguments=prepare(system[0].state,db,actor,body,arguments)
        invocation=Invocation(id=uid(),household_id='h1',owner_id=alice.user_id,task_id=tid,step=0,
            capability='host.inspect@v1',arguments_hash=digest(canonical(arguments)),arguments='',status=invocation_status)
        invocation.arguments=system[0].state.vault.seal(arguments,alice.user_id+':invocation:'+invocation.id)
        db.add(invocation);db.flush()
        approval=Approval(id=uid(),household_id='h1',owner_id=alice.user_id,invocation_id=invocation.id,
            arguments_hash=invocation.arguments_hash,decision=decision,expires_at=now()+(-5 if expired_approval else 300))
        db.add(approval)
        task.status='AWAITING_APPROVAL';task.cancel_requested=canceled
        if expired_task:task.deadline=now()-5
        db.commit()
        return tid,approval.id


async def test_pending_metric_approval_migrates_without_fabricating_user_consent(system,alice,monkeypatch):
    from homeai.task_control import release_unnecessary_approvals
    from homeai.security import Actor
    calls=[]
    async def collect(targets):calls.append(targets);return {'cpu':{'usage_percent':12.0}}
    monkeypatch.setattr('homeai.host_inspection.collect_metrics',collect)
    tid,aid=legacy_pending_inspection(system,alice)
    with system[2]() as db:
        assert release_unnecessary_approvals(system[0].state,db,Actor(alice.user_id,'h1',alice.device_id,'infrastructure_owner'))==1
        db.commit()
        assert db.get(Approval,aid).decision=='NOT_REQUIRED'
        assert db.get(Task,tid).status=='RECEIVED'
    assert calls==[]
    await run_task(system[0].state,tid,alice.user_id)
    assert alice.request('GET','/api/v1/tasks/'+tid).json()['status']=='SUCCEEDED'
    assert calls==[['cpu']]
    with system[2]() as db:
        assert db.get(Approval,aid).decision=='NOT_REQUIRED'


@pytest.mark.parametrize('changes',[
    {'targets':['cpu','screenshot']},{'decision':'REJECTED'}, {'decision':'CANCELED'},
    {'expired_task':True},{'expired_approval':True},{'canceled':True},{'invocation_status':'EXECUTING'},
])
def test_approval_migration_preserves_dangerous_denied_expired_and_started_work(system,alice,changes):
    from homeai.task_control import release_unnecessary_approvals
    from homeai.security import Actor
    tid,aid=legacy_pending_inspection(system,alice,**changes)
    with system[2]() as db:
        assert release_unnecessary_approvals(system[0].state,db,Actor(alice.user_id,'h1',alice.device_id,'infrastructure_owner'))==0
        db.commit()
        assert db.get(Task,tid).status=='AWAITING_APPROVAL'
        assert db.get(Approval,aid).decision==changes.get('decision','PENDING')


async def test_failed_metric_retry_continues_without_new_approval(system,alice,monkeypatch):
    from fastapi import HTTPException
    calls=[]
    async def collect(targets):
        calls.append(targets)
        if len(calls)==1:raise HTTPException(503,'性能采集暂不可用')
        return {'cpu':{'usage_percent':12.0}}
    monkeypatch.setattr('homeai.host_inspection.collect_metrics',collect)
    tid=submit(alice,['cpu'])
    await run_task(system[0].state,tid,alice.user_id)
    assert alice.request('GET','/api/v1/tasks/'+tid).json()['status']=='FAILED'
    result=alice.request('POST','/api/v1/tasks/'+tid+'/retry',{'idempotency_key':str(uuid.uuid4())})
    assert result.status_code==200 and result.json()['status']=='RECEIVED',result.text
    await run_task(system[0].state,tid,alice.user_id)
    assert alice.request('GET','/api/v1/tasks/'+tid).json()['status']=='SUCCEEDED'
    assert calls==[['cpu'],['cpu']]
    assert not any(row['task_id']==tid for row in alice.request('GET','/api/v1/approvals').json())


@pytest.mark.parametrize('message',['请读取我最近24小时的步数','帮我查询我的健康数据','查看服务器CPU和内存','请运行Shell脚本生成文件'])
def test_explicit_live_requests_keep_tools_enabled(message):
    from homeai.planner import requires_live_tools
    assert requires_live_tools(message)
