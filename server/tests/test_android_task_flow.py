"""服务端取得安卓控制权、AI 应用范围与终态事件；仅公开元数据和自动化替身。"""
import json
import uuid
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from conftest import SignedClient
from homeai import android_workspace as android, android_tasks, android_tools
from homeai.crypto import digest
from homeai.db import Task, Invocation, Outbox, Audit, Record
from homeai.runtime import run_task
from homeai.security import Actor
from homeai.task_events import snapshot
from homeai.tool_availability import available_tools
from homeai.planner import requires_live_tools
from test_android_workspace import workspace


def submit(user, capability='android.launch@v1', arguments=None, steps=None):
    body = {'idempotency_key': str(uuid.uuid4()), 'capability': capability,
            'arguments': arguments or {'package': 'com.example.test', 'purpose': '打开公开测试应用'}}
    if steps is not None:
        body.pop('capability'); body.pop('arguments'); body['steps'] = steps
    response = user.request('POST', '/api/v1/tasks', body)
    assert response.status_code == 202, response.text
    return response.json()['id']


def body_of(app, actor, task_id):
    with app.db() as db:
        task = db.get(Task, task_id)
        return app.vault.open(task.request, actor.user_id + ':task:' + task_id)


def test_wildcard_is_explicit_and_cannot_mix_with_names():
    assert android.BindingRequest(serial='public', label='公开设备', packages=[' * ']).packages == ['*']
    assert android.BindingRequest(serial='public', label='公开设备', packages=[]).packages == []
    assert android.normalize_packages(['com.example.test', 'com.example.test']) == ['com.example.test']
    for packages in (['*', 'com.example.test'], ['com.example.*'], [''], [';id']):
        with pytest.raises(ValidationError):
            android.BindingRequest(serial='public', label='公开设备', packages=packages)


async def test_paused_launch_has_no_observation_deadlock_and_releases_terminal(alice, workspace, monkeypatch):
    app, actor, executed = workspace
    reader = AsyncMock(return_value=b'package:/data/app/public/base.apk')
    monkeypatch.setattr(android.android_adb, '_run', reader)
    hierarchy = AsyncMock()
    monkeypatch.setattr(android.android_adb, 'hierarchy', hierarchy)
    task_id = submit(alice)
    await run_task(app, task_id, actor.user_id)
    response = alice.request('GET', '/api/v1/tasks/' + task_id).json()
    assert response['status'] == 'SUCCEEDED', response
    assert response['result']['foreground_verified'] is True
    assert response['used_android'] and response['android_task_id'] == task_id
    assert response['android_workspace_id']
    hierarchy.assert_not_awaited()
    executed.assert_awaited_once()
    assert reader.call_args.args[2] == ['shell', 'pm', 'path', 'com.example.test']
    view = (await android.workspace(app, actor))['workspace']
    assert view['mode'] == 'paused' and view['active_task_id'] is None
    event = snapshot(app, actor, digest(alice.token.encode()), task_id)
    assert event['android_active_task_id'] is None and event['android_revision'] > 0
    assert event['tasks'][0]['used_android'] and event['tasks'][0]['android_task_id'] == task_id
    with app.db() as db:
        assert db.scalar(select(Record).where(Record.source == 'android_observation')) is None
        actions = set(db.scalars(select(Audit.action).where(Audit.owner_id == actor.user_id)))
        assert {'android.task.started', 'android.task.ended', 'android.app.launch'} <= actions


async def test_active_task_is_visible_before_completion_and_private_to_member(system, alice, workspace):
    app, actor, _ = workspace
    task_id = submit(alice)
    body = body_of(app, actor, task_id)
    binding = await android_tasks.acquire(app, actor, task_id, body)
    view = (await android.workspace(app, actor))['workspace']
    assert view['mode'] == 'ai' and view['active_task_id'] == task_id
    assert view['active_task_status'] == 'RECEIVED'
    event = snapshot(app, actor, digest(alice.token.encode()), task_id)
    assert event['android_active_task_id'] == task_id
    bob = SignedClient(system[1], system[2])
    other = Actor(bob.user_id, 'h1', bob.device_id, 'adult')
    assert (await android.workspace(app, other))['workspace'] is None
    assert snapshot(app, other, digest(bob.token.encode()), None)['android_active_task_id'] is None


async def test_human_cannot_be_preempted_and_old_task_cannot_reacquire(alice, workspace):
    app, actor, executed = workspace
    task_id = submit(alice)
    body = body_of(app, actor, task_id)
    await android_tasks.acquire(app, actor, task_id, body)
    await android.control(app, actor, android.ControlRequest(mode='human', expected_epoch=2))
    with pytest.raises(HTTPException) as human:
        await android_tasks.acquire(app, actor, task_id, body)
    assert '没有抢占' in human.value.detail
    await android.control(app, actor, android.ControlRequest(mode='ai', expected_epoch=3))
    with pytest.raises(HTTPException) as stale:
        await android_tasks.acquire(app, actor, task_id, body)
    assert stale.value.status_code == 409
    executed.assert_not_awaited()


async def test_empty_ai_scope_hides_tools_but_does_not_restrict_human(workspace, monkeypatch):
    app, actor, executed = workspace
    with app.db() as db:
        row = android.get_binding(app, db, actor)
        row.packages = '[]'
        db.commit()
        tools, _ = available_tools(app, db, actor)
        assert not {'open_android_app', 'observe_android', 'operate_android'} & {item['function']['name'] for item in tools}
    await android.control(app, actor, android.ControlRequest(mode='human', expected_epoch=1))
    _, frame = await android.capture(app, actor)
    assert (await android.input_event(app, actor, android.InputRequest(epoch=frame['epoch'], sequence=1,
        frame_id=frame['frame_id'], action='launch', package='com.personal.other')))['status'] == 'succeeded'
    executed.assert_awaited_once()


async def test_ai_list_enforces_launch_and_current_foreground(workspace, monkeypatch):
    app, actor, executed = workspace
    await android.control(app, actor, android.ControlRequest(mode='ai', expected_epoch=1))
    _, frame = await android.capture(app, actor, source='ai')
    monkeypatch.setattr(android, 'foreground_package', AsyncMock(return_value='com.other.app'))
    for action, arguments in [('launch', {'package': 'com.other.app'}), ('home', {}),
                              ('tap', {'x': 0.5, 'y': 0.5})]:
        with pytest.raises(HTTPException) as denied:
            await android.input_event(app, actor, android.InputRequest(epoch=2, sequence=1,
                frame_id=frame['frame_id'], action=action, **arguments), source='ai')
        assert denied.value.status_code == 403
    executed.assert_not_awaited()


async def test_wildcard_allows_ai_app_without_lowering_payment_boundary(workspace):
    app, actor, _ = workspace
    with app.db() as db:
        row = android.get_binding(app, db, actor)
        row.packages = '["*"]'
        db.commit()
        tools, _ = available_tools(app, db, actor)
        assert 'open_android_app' in {item['function']['name'] for item in tools}
        prepared = android_tools.prepare(app, db, actor, {}, 'android.launch@v1', {
            'package': 'com.another.app', 'purpose': '打开允许应用'})
        assert prepared['package'] == 'com.another.app'
    raw = '<hierarchy><node text="立即支付" clickable="true" bounds="[0,0][320,640]"/></hierarchy>'.encode()
    result = android_tools.project_hierarchy(raw, 320, 640)
    assert result['manual_required'] is False
    from homeai.android_action_guard import target, AndroidAssistanceRequired
    with pytest.raises(AndroidAssistanceRequired):
        target(raw, 320, 640, android.InputRequest(epoch=1, sequence=1, frame_id='f'*24, action='tap', x=0.5, y=0.5))


async def test_uninstalled_app_has_clear_failure_and_no_dispatched_marker(alice, workspace, monkeypatch):
    app, actor, executed = workspace
    monkeypatch.setattr(android.android_adb, '_run', AsyncMock(return_value=b''))
    task_id = submit(alice)
    await run_task(app, task_id, actor.user_id)
    response = alice.request('GET', '/api/v1/tasks/' + task_id).json()
    assert response['status'] == 'FAILED' and '尚未安装' in response['error']
    assert '_android_dispatched' not in body_of(app, actor, task_id)
    executed.assert_not_awaited()
    assert (await android.workspace(app, actor))['workspace']['active_task_id'] is None


async def test_launch_offline_requests_help_before_package_lookup_or_dispatch(alice, workspace, monkeypatch):
    app, actor, executed = workspace
    task_id = submit(alice)
    body = body_of(app, actor, task_id)
    await android_tasks.acquire(app, actor, task_id, body)
    lookup = AsyncMock(return_value=b'package:/data/app/public/base.apk')
    monkeypatch.setattr(android.android_adb, '_run', lookup)
    monkeypatch.setattr(android.android_adb, 'devices', AsyncMock(return_value=[]))
    dispatched = []
    from homeai.android_action_guard import AndroidAssistanceRequired
    with pytest.raises(AndroidAssistanceRequired) as error:
        await android.launch_for_task(app, actor, 'com.example.test', task_id=task_id,
            expected_epoch=body['_android_epoch'], before_dispatch=lambda: dispatched.append(True))
    assert error.value.code == 'device_offline'
    assert dispatched == []
    lookup.assert_not_awaited()
    executed.assert_not_awaited()
    assert '_android_dispatched' not in body_of(app, actor, task_id)


@pytest.mark.parametrize('message', ['帮我点外卖', '帮我打车', '打开美团', '打开安卓系统设置'])
def test_app_operation_requests_require_real_tools(message):
    assert requires_live_tools(message)


async def test_empty_scope_reports_bound_device_instead_of_repairing(workspace):
    app, actor, _ = workspace
    with app.db() as db:
        row = android.get_binding(app, db, actor)
        row.packages = '[]'
        db.commit()
        _, reasons = available_tools(app, db, actor)
        assert reasons['open_android_app'] == '已绑定，但未允许AI操作任何App，请在成员设备配置包名或*；本人仍可接管'
        with pytest.raises(HTTPException) as error:
            android_tools.prepare(app, db, actor, {}, 'android.launch@v1', {'package': 'com.example.test', 'purpose': '公开测试'})
        assert '已绑定' in error.value.detail and '未允许AI' in error.value.detail


async def test_foreground_launch_scope_is_rechecked_against_task_and_epoch(alice, workspace):
    app, actor, executed = workspace
    task_id = submit(alice)
    body = body_of(app, actor, task_id)
    await android_tasks.acquire(app, actor, task_id, body)
    await android.control(app, actor, android.ControlRequest(mode='human', expected_epoch=2))
    await android.control(app, actor, android.ControlRequest(mode='ai', expected_epoch=3))
    other_id = submit(alice)
    await android_tasks.acquire(app, actor, other_id, body_of(app, actor, other_id))
    called = []
    with pytest.raises(HTTPException):
        await android.launch_for_task(app, actor, 'com.example.test', task_id=task_id, expected_epoch=2,
            before_dispatch=lambda: called.append(True))
    assert called == []
    executed.assert_not_awaited()


async def test_explicit_task_cancel_releases_workspace_without_waiting_worker(alice, workspace):
    app, actor, _ = workspace
    task_id = submit(alice)
    await android_tasks.acquire(app, actor, task_id, body_of(app, actor, task_id))
    response = alice.request('POST', '/api/v1/tasks/' + task_id + '/cancel')
    assert response.status_code == 200 and response.json()['status'] == 'CANCELED'
    view = (await android.workspace(app, actor))['workspace']
    assert view['mode'] == 'paused' and view['active_task_id'] is None
    assert response.json()['used_android']


async def test_multistep_launch_then_observe_runs_only_allowed_app(alice, workspace, monkeypatch):
    app, actor, executed = workspace
    current = {'package': 'com.launcher.public'}
    async def foreground(*_): return current['package']
    async def execute(*_, **kwargs): current['package'] = 'com.example.test'; return b''
    monkeypatch.setattr(android, 'foreground_package', foreground)
    monkeypatch.setattr(android.android_adb, '_run', AsyncMock(return_value=b'package:/data/app/public/base.apk'))
    executed.side_effect = execute
    hierarchy = AsyncMock(return_value=b'<hierarchy><node package="com.example.test" text="Public" bounds="[0,0][30,30]"/></hierarchy>')
    monkeypatch.setattr(android.android_adb, 'hierarchy', hierarchy)
    task_id = submit(alice, steps=[
        {'capability': 'android.launch@v1', 'arguments': {'package': 'com.example.test', 'purpose': '公开测试启动'}},
        {'capability': 'android.observe@v1', 'arguments': {'purpose': '核对公开页面'}}])
    await run_task(app, task_id, actor.user_id)
    assert (await android.workspace(app, actor))['workspace']['active_task_id'] == task_id
    hierarchy.assert_not_awaited()
    await run_task(app, task_id, actor.user_id)
    response = alice.request('GET', '/api/v1/tasks/' + task_id).json()
    assert response['status'] == 'SUCCEEDED' and response['used_android']
    assert response['result']['nodes'][0]['text'] == 'Public'
    hierarchy.assert_awaited_once()
    assert (await android.workspace(app, actor))['workspace']['mode'] == 'paused'


async def test_ai_observation_outside_scope_reads_no_screen_or_hierarchy(workspace, monkeypatch):
    app, actor, _ = workspace
    await android.control(app, actor, android.ControlRequest(mode='ai', expected_epoch=1))
    monkeypatch.setattr(android, 'foreground_package', AsyncMock(return_value='com.outside.app'))
    screenshot, hierarchy = AsyncMock(), AsyncMock()
    monkeypatch.setattr(android.android_adb, 'screenshot', screenshot)
    monkeypatch.setattr(android.android_adb, 'hierarchy', hierarchy)
    raw, metadata = await android.observe(app, actor)
    assert raw == b'<hierarchy/>' and metadata['scope_blocked']
    assert metadata['frame_id'] is None
    screenshot.assert_not_awaited(); hierarchy.assert_not_awaited()


async def test_task_waiting_for_human_control_can_retry_without_replaying_effects(alice, workspace, monkeypatch):
    app, actor, executed = workspace
    monkeypatch.setattr(android.android_adb, '_run', AsyncMock(return_value=b'package:/data/app/public/base.apk'))
    await android.control(app, actor, android.ControlRequest(mode='human', expected_epoch=1))
    task_id = submit(alice)
    await run_task(app, task_id, actor.user_id)
    failed = alice.request('GET', '/api/v1/tasks/' + task_id).json()
    assert failed['status'] == 'FAILED' and '没有抢占' in failed['error']
    executed.assert_not_awaited()
    from homeai.task_control import retry_status
    with app.db() as db:
        verdict = retry_status(app, db, actor, db.get(Task, task_id))
    assert verdict['can_retry']
    await android.control(app, actor, android.ControlRequest(mode='paused', expected_epoch=2))
    response = alice.request('POST', '/api/v1/tasks/' + task_id + '/retry', {'idempotency_key': str(uuid.uuid4())})
    assert response.status_code == 200, response.text
    await run_task(app, task_id, actor.user_id)
    assert alice.request('GET', '/api/v1/tasks/' + task_id).json()['status'] == 'SUCCEEDED'
    executed.assert_awaited_once()
