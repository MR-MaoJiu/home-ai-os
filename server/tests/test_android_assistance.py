"""真实任务账本验证安卓求助、继续与旧计划失效；设备替身仅用于测试。"""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from conftest import SignedClient
from homeai import android_workspace as android
from homeai.android_assistance import request_assistance, recover_observation, continue_android, AndroidContinue
from homeai.client_actions import ClientAction, create_request, expire_requests
from homeai.db import Device, Invocation, Task, Notification, now, scope
from homeai.security import Actor
from test_android_workspace import workspace
from test_client_actions import new_task


async def blocked(system, alice, workspace, *, agent=True, dispatched=False):
    app, actor, _ = workspace
    tid = new_task(system, alice)
    with app.db() as db:
        task = db.get(Task, tid)
        body = app.vault.open(task.request, actor.user_id + ':task:' + tid)
        row = android.get_binding(app, db, actor)
        row.mode, row.active_task_id = 'ai', tid
        body.update(_agent=agent, _android_epoch=row.epoch, _android_task_id=tid, _android_workspace_id=row.id,
            _android_focus={'old-frame': 'old-token'}, _android_observation={'old': True},
            steps=[{'capability': 'android.action@v1', 'arguments': {'input': {'action': 'tap'}, 'purpose': '公开测试', 'impact_summary': '打开测试页'}},
                   {'capability': 'android.action@v1', 'arguments': {'input': {'action': 'back'}, 'purpose': '公开测试', 'impact_summary': '返回'}}])
        task.request = app.vault.seal(body, actor.user_id + ':task:' + tid)
        db.commit()
        if dispatched:
            inv = Invocation(owner_id=actor.user_id, household_id=actor.household_id, task_id=tid, step=0,
                capability='android.action@v1', arguments_hash='public', arguments='public', status='EXECUTING')
            db.add(inv); db.flush(); body['_android_dispatched'] = inv.id
        else:
            inv = None
        await request_assistance(app, db, actor, task, body, inv, 0, '当前页面需要输入密码，请处理后继续。', 'password')
        db.commit()
        aid = db.scalar(select(ClientAction.id).where(ClientAction.task_id == tid))
    return tid, aid


def resume(alice, aid, key=None):
    return alice.request('POST', '/api/v1/client-actions/' + aid + '/android-continue',
                         {'idempotency_key': key or str(uuid.uuid4())})


async def test_help_human_takeover_then_resume_is_same_task_and_never_replays(system, alice, workspace):
    app, actor, executed = workspace
    tid, aid = await blocked(system, alice, workspace)
    assert aid
    with app.db() as db:
        row = android.get_binding(app, db, actor)
        assert row.mode == 'paused' and row.active_task_id is None
        epoch = row.epoch
        assert db.get(Task, tid).status == 'WAITING_CLIENT'
        assert db.scalar(select(Notification).where(Notification.event_key == 'client-action:' + aid))
    assert alice.request('POST', '/api/v1/client-actions/' + aid + '/respond', {'idempotency_key': str(uuid.uuid4()), 'text': '123456'}).status_code == 422
    await android.control(app, actor, android.ControlRequest(mode='human', expected_epoch=epoch))
    key = str(uuid.uuid4())
    first = resume(alice, aid, key)
    assert first.status_code == 200, first.text
    assert first.json()['status'] == 'RESPONDED'
    assert resume(alice, aid, key).json() == first.json()
    assert resume(alice, aid).status_code == 409
    with app.db() as db:
        task = db.get(Task, tid)
        assert task.status == 'RECEIVED' and not task.cancel_requested
        body = app.vault.open(task.request, actor.user_id + ':task:' + tid)
        assert '_android_focus' not in body and '_android_observation' not in body
        assert body['_android_needs_observation'] is True
        assert len(body['steps']) == 3 and body['steps'][-1]['capability'] == 'android.observe@v1'
        assert [item.status for item in db.scalars(select(Invocation).where(Invocation.task_id == tid).order_by(Invocation.step))] == ['SKIPPED', 'SKIPPED']
        row = android.get_binding(app, db, actor)
        assert row.epoch > epoch and row.active_task_id == tid and body['_android_epoch'] == row.epoch
    executed.assert_not_awaited()


@pytest.mark.parametrize('change', ['binding', 'origin_revoked', 'expired', 'another_ai', 'unknown_result', 'canceled'])
async def test_resume_rejects_changed_authority_and_does_not_touch_device(system, alice, workspace, monkeypatch, change):
    app, actor, _ = workspace
    tid, aid = await blocked(system, alice, workspace)
    with app.db() as db:
        row = android.get_binding(app, db, actor)
        if change == 'binding': row.packages = '["*"]'
        if change == 'origin_revoked':
            # 测试当前有效设备响应另一个已撤销的原发起设备。
            original = Device(user_id=actor.user_id, public_key='public', name='旧设备', revoked=True)
            db.add(original); db.flush()
            task = db.get(Task, tid); body = app.vault.open(task.request, actor.user_id + ':task:' + tid)
            body['device_id'] = original.id; task.request = app.vault.seal(body, actor.user_id + ':task:' + tid)
        if change == 'expired': db.get(ClientAction, aid).expires_at = now() - 1
        if change == 'another_ai': row.mode, row.active_task_id = 'ai', 'other-task'
        if change == 'unknown_result': db.scalar(select(Invocation).where(Invocation.task_id == tid)).status = 'NEEDS_RECONCILIATION'
        if change == 'canceled': db.get(Task, tid).cancel_requested = True
        db.commit()
    entered = []
    @asynccontextmanager
    async def barrier(*args):
        entered.append(True)
        yield
    monkeypatch.setattr('homeai.android_gestures.gesture_barrier', barrier)
    result = resume(alice, aid)
    assert result.status_code in {403, 409}, result.text
    assert not entered
    assert alice.request('GET', '/api/v1/client-actions/' + aid).json()['status'] in {'PENDING', 'EXPIRED'}


async def test_another_member_and_other_device_human_do_not_interrupt(system, alice, workspace, monkeypatch):
    app, actor, _ = workspace
    tid, aid = await blocked(system, alice, workspace)
    bob = SignedClient(system[1], system[2])
    assert resume(bob, aid).status_code == 404
    with app.db() as db:
        other = Device(user_id=actor.user_id, public_key='public', name='其他设备')
        db.add(other); db.flush()
        row = android.get_binding(app, db, actor)
        row.mode, row.controller_device_id, row.lease_until = 'human', other.id, now()+30
        db.commit()
    entered = []
    @asynccontextmanager
    async def barrier(*args):
        entered.append(True); yield
    monkeypatch.setattr('homeai.android_gestures.gesture_barrier', barrier)
    assert resume(alice, aid).status_code == 409
    assert not entered


async def test_cancel_and_expire_end_only_waiting_task(system, alice, workspace):
    app, actor, _ = workspace
    tid, aid = await blocked(system, alice, workspace)
    payload = {'idempotency_key': str(uuid.uuid4())}
    denied = alice.request('POST', '/api/v1/client-actions/' + aid + '/deny', payload)
    assert denied.status_code == 200 and denied.json()['status'] == 'CANCELED'
    assert alice.request('POST', '/api/v1/client-actions/' + aid + '/deny', payload).json() == denied.json()
    assert resume(alice, aid).status_code == 409
    with app.db() as db:
        assert db.get(Task, tid).status == 'CANCELED'
    tid, aid = await blocked(system, alice, workspace)
    with app.db() as db: db.get(ClientAction, aid).expires_at = now()-1; db.commit()
    expire_requests(app)
    with app.db() as db:
        assert db.get(ClientAction, aid).status == 'EXPIRED'
        assert db.get(Task, tid).status == 'FAILED'
    assert resume(alice, aid).status_code == 409


async def test_nonagent_never_reports_unexecuted_tap_as_success_and_model_cannot_create_help(system, alice, workspace):
    app, actor, _ = workspace
    tid, aid = await blocked(system, alice, workspace, agent=False)
    assert aid is None
    with app.db() as db:
        task = db.get(Task, tid)
        assert task.status == 'FAILED' and '未执行' in task.error
    tid = new_task(system, alice)
    with app.db() as db:
        with pytest.raises(HTTPException):
            create_request(app, db, actor, db.get(Task, tid), 'android.assist', '伪造求助', {'workspace_id':'fake','reason':'fake','code':'fake'})


async def test_dispatched_input_cannot_be_withdrawn(system, alice, workspace):
    with pytest.raises(HTTPException):
        await blocked(system, alice, workspace, dispatched=True)


async def test_observation_refresh_is_bounded_and_cancel_never_resurrects(system, alice, workspace):
    app, actor, _ = workspace
    tid = new_task(system, alice)
    with app.db() as db:
        task = db.get(Task, tid)
        body = app.vault.open(task.request, actor.user_id + ':task:' + tid)
        row = android.get_binding(app, db, actor)
        row.mode, row.active_task_id = 'ai', tid
        body.update(_agent=True, _android_task_id=tid, _android_workspace_id=row.id, _android_epoch=row.epoch,
            steps=[{'capability':'android.action@v1','arguments':{}}])
        task.request=app.vault.seal(body,actor.user_id+':task:'+tid);db.commit()
        for attempt in range(3):
            index=len(body['steps'])-1
            await recover_observation(app,db,actor,task,body,None,index,'画面已变化','screen_changed')
            db.commit()
            body=app.vault.open(task.request,actor.user_id+':task:'+tid)
            assert body['_android_observation_retries']==attempt+1
        assert task.status=='WAITING_CLIENT'
        assert db.scalar(select(ClientAction).where(ClientAction.task_id==tid)).kind=='android.assist'
        task.cancel_requested=True;task.status='CANCELED';db.commit()
        await recover_observation(app,db,actor,task,body,None,0,'画面已变化','screen_changed')
        assert task.status=='CANCELED'


@pytest.mark.skipif(__import__('os').environ.get('HOMEAI_INTEGRATION') != '1', reason='仅独立 homeai_test PostgreSQL 数据库')
def test_postgres_duplicate_continue_serializes_without_deadlock(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from homeai import android_adb
    from homeai.config import Settings
    from homeai.crypto import Vault
    from homeai.db import Base, Principal, database
    from homeai.runtime import submit
    from homeai.contracts import TaskRequest
    settings=Settings(state_dir=tmp_path)
    engine,factory=database(settings.database_url.rsplit('/',1)[0]+'/homeai_test')
    member,family,device_id,serial=[str(uuid.uuid4()) for _ in range(4)]
    actor=Actor(member,family,device_id,'adult')
    app=SimpleNamespace(db=factory,vault=Vault(b'p'*32),settings=settings)
    monkeypatch.setattr(android_adb,'load_policy',lambda _:android_adb.AndroidPolicy(enabled=True,adb_binary='/usr/bin/true',serials=[serial]))
    try:
        with factory() as db:
            db.add(Principal(id=member,household_id=family,role='adult',name='安卓协助并发公开测试'))
            db.add(Device(id=device_id,user_id=member,public_key='public-test',name='并发设备'))
            db.commit();scope(db,member,family)
            row=android.AndroidWorkspace(owner_id=member,household_id=family,serial=serial,label='公开测试设备',backend='emulator',packages='["com.example.test"]')
            db.add(row);db.flush()
            task=submit(db,actor,TaskRequest(idempotency_key=str(uuid.uuid4()),message='公开任务'),app.vault)
            tid=task.id
            body=app.vault.open(task.request,member+':task:'+tid)
            body.update(_agent=True,_android_epoch=row.epoch,_android_task_id=tid,_android_workspace_id=row.id,
                steps=[{'capability':'android.action@v1','arguments':{}}])
            row.mode,row.active_task_id='ai',tid
            task.request=app.vault.seal(body,member+':task:'+tid);db.commit()
            asyncio.run(request_assistance(app,db,actor,task,body,None,0,'需要输入密码','password'))
            db.commit();aid=db.scalar(select(ClientAction.id).where(ClientAction.task_id==tid))
        payload=AndroidContinue(idempotency_key=str(uuid.uuid4()))
        request=SimpleNamespace(app=SimpleNamespace(state=app))
        def invoke():
            return asyncio.run(continue_android(aid,payload,request,actor))
        with ThreadPoolExecutor(max_workers=2) as pool:
            first,second=pool.submit(invoke),pool.submit(invoke)
            assert first.result(timeout=10)==second.result(timeout=10)
        with factory() as db:
            scope(db,member,family)
            task=db.get(Task,tid)
            saved=app.vault.open(task.request,member+':task:'+tid)
            assert task.status=='RECEIVED' and len(saved['steps'])==2
            assert saved['steps'][-1]['capability']=='android.observe@v1'
            assert db.get(ClientAction,aid).status=='RESPONDED'
    finally:
        with factory() as db:
            scope(db,member,family)
            for table in reversed(Base.metadata.sorted_tables):
                if 'owner_id' in table.c and 'household_id' in table.c:
                    db.execute(table.delete().where(table.c.owner_id==member,table.c.household_id==family))
            db.query(Device).filter(Device.id==device_id).delete()
            db.query(Principal).filter(Principal.id==member).delete()
            db.commit()
        engine.dispose()


@pytest.mark.parametrize('waiting', [True, False])
async def test_runtime_preflight_blocks_before_approval_and_persists_recovery(system, alice, workspace, monkeypatch, waiting):
    from unittest.mock import AsyncMock
    from homeai.runtime import _run_step
    from homeai.android_action_guard import AndroidAssistanceRequired, AndroidNeedsObservation
    from homeai.db import Approval
    app, actor, executed = workspace
    tid = new_task(system, alice)
    with app.db() as db:
        task=db.get(Task,tid);body=app.vault.open(task.request,actor.user_id+':task:'+tid)
        row=android.get_binding(app,db,actor);row.mode,row.active_task_id='ai',tid
        body.update(_agent=True,_android_epoch=row.epoch,_android_task_id=tid,_android_workspace_id=row.id,
            steps=[{'capability':'android.action@v1','arguments':{}}])
        task.request=app.vault.seal(body,actor.user_id+':task:'+tid);db.commit()
    monkeypatch.setattr('homeai.android_tools.prepare',lambda *args: {})
    error=AndroidAssistanceRequired('当前需要密码','password') if waiting else AndroidNeedsObservation()
    monkeypatch.setattr('homeai.android_action_guard.preflight',AsyncMock(side_effect=error))
    await _run_step(app,tid,actor.user_id)
    with app.db() as db:
        task=db.get(Task,tid)
        assert task.status==('WAITING_CLIENT' if waiting else 'RECEIVED'),task.error
        invocation=db.scalar(select(Invocation).where(Invocation.task_id==tid))
        assert invocation.status=='SKIPPED'
        assert not db.scalar(select(Approval).where(Approval.invocation_id==invocation.id))
    executed.assert_not_awaited()
