"""Android 控制权隔离、帧校验与结果不明不重放。设备替身仅用于自动化故障测试。"""
import asyncio
import json
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from homeai import android_adb, android_workspace as android
from homeai.db import Device, now
from homeai.security import Actor
from conftest import SignedClient


def png(color=b'public'):
    return b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + b'IHDR' + struct.pack('>II', 320, 640) + b'\x08\x02\0\0\0' + color


@pytest.fixture
async def workspace(system, alice, monkeypatch):
    app = system[0].state
    policy = android_adb.AndroidPolicy(enabled=True, adb_binary='/usr/bin/true', serials=['emulator-5554'])
    monkeypatch.setattr(android_adb, 'load_policy', lambda settings: policy)
    monkeypatch.setattr(android_adb, 'devices', AsyncMock(return_value=[{
        'serial': 'emulator-5554', 'label': '公开测试设备', 'backend': 'emulator', 'status': 'online'}]))
    monkeypatch.setattr(android_adb, 'screenshot', AsyncMock(return_value=(png(), 320, 640)))
    monkeypatch.setattr(android, 'foreground_package', AsyncMock(return_value='com.example.test'))
    from homeai import android_action_guard
    monkeypatch.setattr(android_action_guard, 'window_fingerprint', AsyncMock(return_value='public-test-window'))
    executed = AsyncMock(return_value=b'')
    monkeypatch.setattr(android_adb, 'execute', executed)
    actor = Actor(alice.user_id, 'h1', alice.device_id, 'infrastructure_owner')
    with app.db() as db:
        row = android.AndroidWorkspace(owner_id=actor.user_id, household_id='h1', serial='emulator-5554',
            label='公开测试设备', backend='emulator', packages='["com.example.test"]')
        db.add(row)
        db.commit()
    return app, actor, executed


@pytest.mark.asyncio
async def test_member_isolation_and_signed_device_required(system, workspace):
    app, actor, _ = workspace
    bob = SignedClient(system[1], system[2])
    other = Actor(bob.user_id, 'h1', bob.device_id, 'adult')
    assert (await android.workspace(app, other))['workspace'] is None
    with pytest.raises(HTTPException) as denied:
        await android.capture(app, other)
    assert denied.value.status_code == 404
    with pytest.raises(HTTPException) as browser:
        await android.workspace(app, Actor(actor.user_id, 'h1', 'browser:session', actor.role))
    assert browser.value.status_code == 401


@pytest.mark.asyncio
async def test_human_lease_and_takeover_invalidate_ai_and_old_inputs(workspace):
    app, actor, _ = workspace
    started = await android.control(app, actor, android.ControlRequest(mode='human', expected_epoch=1))
    epoch = started['workspace']['epoch']
    renewed = await android.control(app, actor, android.ControlRequest(mode='human', expected_epoch=epoch))
    assert renewed['workspace']['epoch'] == epoch
    assert renewed['workspace']['lease_until'] > now()
    with pytest.raises(HTTPException):
        await android.capture(app, actor, source='ai')
    await android.control(app, actor, android.ControlRequest(mode='ai', expected_epoch=epoch))
    _, frame = await android.capture(app, actor, source='ai')
    assert frame['mode'] == 'ai' and frame['epoch'] == epoch + 1
    with pytest.raises(HTTPException) as stale:
        await android.input_event(app, actor, android.InputRequest(epoch=epoch, sequence=1,
            frame_id='x' * 24, action='back'))
    assert stale.value.status_code == 409


@pytest.mark.asyncio
async def test_identical_screen_frame_reused_and_input_idempotent(workspace):
    app, actor, executed = workspace
    await android.control(app, actor, android.ControlRequest(mode='human', expected_epoch=1))
    _, first = await android.capture(app, actor)
    _, second = await android.capture(app, actor)
    assert first['frame_id'] == second['frame_id']
    request = android.InputRequest(epoch=second['epoch'], sequence=1, frame_id=first['frame_id'], action='tap', x=0.5, y=0.5)
    assert (await android.input_event(app, actor, request))['status'] == 'succeeded'
    await android.input_event(app, actor, request)
    assert executed.await_count == 1
    assert (await android.workspace(app, actor))['workspace']['last_sequence'] == 1
    with pytest.raises(HTTPException):
        await android.input_event(app, actor, request.model_copy(update={'x': 0.4}))
    assert executed.await_count == 1


@pytest.mark.asyncio
async def test_changed_screen_rejected_but_human_launch_is_not_ai_scoped(workspace, monkeypatch):
    app, actor, executed = workspace
    await android.control(app, actor, android.ControlRequest(mode='human', expected_epoch=1))
    _, frame = await android.capture(app, actor)
    assert (await android.input_event(app, actor, android.InputRequest(epoch=frame['epoch'], sequence=1,
        frame_id=frame['frame_id'], action='launch', package='com.other.app')))['status'] == 'succeeded'
    _, frame = await android.capture(app, actor)
    monkeypatch.setattr(android_adb, 'screenshot', AsyncMock(return_value=(png(b'changed'), 320, 640)))
    with pytest.raises(HTTPException) as screen:
        await android.input_event(app, actor, android.InputRequest(epoch=frame['epoch'], sequence=2,
            frame_id=frame['frame_id'], action='tap', x=0.5, y=0.5))
    assert screen.value.status_code == 409
    assert executed.await_count == 1


@pytest.mark.asyncio
async def test_unknown_execution_is_never_replayed(workspace):
    app, actor, executed = workspace
    await android.control(app, actor, android.ControlRequest(mode='human', expected_epoch=1))
    _, frame = await android.capture(app, actor)
    request = android.InputRequest(epoch=frame['epoch'], sequence=1, frame_id=frame['frame_id'], action='back')
    executed.side_effect = HTTPException(504, '设备超时')
    with pytest.raises(HTTPException):
        await android.input_event(app, actor, request)
    with pytest.raises(HTTPException) as retry:
        await android.input_event(app, actor, request)
    assert retry.value.status_code == 409 and executed.await_count == 1
    with app.db() as db:
        assert db.scalar(select(android.AndroidInput)).status == 'UNKNOWN'


@pytest.mark.asyncio
async def test_expired_lease_and_revoked_device_pause_without_ai_resume(workspace):
    app, actor, _ = workspace
    await android.control(app, actor, android.ControlRequest(mode='human', expected_epoch=1))
    with app.db() as db:
        row = db.scalar(select(android.AndroidWorkspace))
        row.lease_until = now() - 1
        db.commit()
    view = (await android.workspace(app, actor))['workspace']
    assert view['mode'] == 'paused' and view['epoch'] == 3
    with app.db() as db:
        db.get(Device, actor.device_id).revoked = True
        db.commit()
    with pytest.raises(HTTPException) as error:
        await android.capture(app, actor)
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_takeover_waits_for_started_input_and_fences_next_input(workspace):
    app, actor, executed = workspace
    await android.control(app, actor, android.ControlRequest(mode='human', expected_epoch=1))
    _, frame = await android.capture(app, actor)
    started, release = asyncio.Event(), asyncio.Event()
    async def slow(*args, **kwargs):
        started.set()
        await release.wait()
    executed.side_effect = slow
    action = asyncio.create_task(android.input_event(app, actor, android.InputRequest(epoch=frame['epoch'],
        sequence=1, frame_id=frame['frame_id'], action='back')))
    await started.wait()
    takeover = asyncio.create_task(android.control(app, actor,
        android.ControlRequest(mode='paused', expected_epoch=frame['epoch'])))
    await asyncio.sleep(0.05)
    assert not takeover.done()
    release.set()
    await action
    result = await takeover
    assert result['workspace']['mode'] == 'paused'
    assert result['workspace']['epoch'] == frame['epoch'] + 1


def test_input_contract_rejects_shell_and_ambiguous_actions():
    values = {'epoch': 1, 'sequence': 1, 'frame_id': 'x' * 24}
    for extra in ({'action': 'shell', 'text': 'id'}, {'action': 'tap', 'x': 2.0, 'y': 0.1},
                  {'action': 'home', 'text': 'hidden'}, {'action': 'text', 'text': 'a\nb'},
                  {'action': 'launch', 'package': 'com.app;id'}):
        with pytest.raises(ValidationError):
            android.InputRequest(**values, **extra)


@pytest.mark.asyncio
async def test_adb_shell_input_never_interprets_metacharacters(tmp_path, monkeypatch):
    policy = android_adb.AndroidPolicy(enabled=True, adb_binary='/usr/bin/true', serials=['emulator-5554'])
    monkeypatch.setattr(android_adb, 'load_policy', lambda settings: policy)
    runner = AsyncMock(return_value=b'')
    monkeypatch.setattr(android_adb, '_run', runner)
    for text in ('hello;id', '$(id)', '`id`', 'hello%s', '世界'):
        with pytest.raises(HTTPException):
            await android_adb.execute(None, 'emulator-5554', SimpleNamespace(action='text', text=text))
    assert runner.await_count == 0
    await android_adb.execute(None, 'emulator-5554', SimpleNamespace(action='text', text='public test'))
    assert runner.call_args.args[2] == ['shell', 'input', 'text', 'public%stest']


@pytest.mark.asyncio
async def test_utf8_ime_is_opt_in_checks_component_and_result(monkeypatch):
    policy = android_adb.AndroidPolicy(enabled=True, adb_binary='/usr/bin/true',
        serials=['emulator-5554'], ime_serials=['emulator-5554'])
    monkeypatch.setattr(android_adb, 'load_policy', lambda settings: policy)
    runner = AsyncMock(side_effect=[b'other/.IME', b'dev.homeai.androidinput/.HomeAIInputService', b'Broadcast completed: result=1'])
    monkeypatch.setattr(android_adb, '_run', runner)
    action = SimpleNamespace(action='text', text='公开中文')
    with pytest.raises(HTTPException) as wrong:
        await android_adb.execute(None, 'emulator-5554', action)
    assert wrong.value.status_code == 409
    await android_adb.execute(None, 'emulator-5554', action)
    assert runner.call_args.args[2][-2] == 'text_base64'
    assert '公开中文' not in str(runner.call_args)


def test_android_policy_requires_private_regular_file(tmp_path):
    settings = SimpleNamespace(state_dir=tmp_path)
    assert android_adb.load_policy(settings).enabled is False
    path = tmp_path / 'android-policy.json'
    path.write_text(json.dumps({'enabled': False, 'serials': []}))
    path.chmod(0o644)
    with pytest.raises(HTTPException):
        android_adb.load_policy(settings)
    path.chmod(0o600)
    assert android_adb.load_policy(settings).enabled is False


def test_binding_api_metadata_and_revoked_serial_remains_private(system, alice, workspace):
    app, actor, _ = workspace
    bob = SignedClient(system[1], system[2])
    body = {'serial': 'emulator-5554', 'label': '公开测试设备', 'packages': ['com.example.test']}
    response = bob.request('GET', '/api/v1/manage/android/bindings')
    assert response.status_code == 403
    response = alice.request('PUT', '/api/v1/manage/android/bindings/' + bob.user_id, body)
    assert response.status_code == 409
    assert alice.request('DELETE', '/api/v1/manage/android/bindings/' + alice.user_id).json() == {'ok': True}
    assert alice.request('GET', '/api/v1/android/workspace').json()['workspace'] is None
    assert alice.request('GET', '/api/v1/manage/android/bindings').json()['bindings'] == []
    assert alice.request('PUT', '/api/v1/manage/android/bindings/' + bob.user_id, body).status_code == 409
    assert alice.request('PUT', '/api/v1/manage/android/bindings/' + alice.user_id, body).status_code == 200
    own = alice.request('GET', '/api/v1/android/workspace').json()['workspace']
    assert own['mode'] == 'paused'
    assert 'serial' not in own and 'frame_digest' not in own


@pytest.mark.asyncio
async def test_ai_observation_is_fenced_by_manual_takeover(workspace, monkeypatch):
    app, actor, _ = workspace
    hierarchy = AsyncMock(return_value='<?xml version="1.0"?><hierarchy><node text="公开示例"/></hierarchy>'.encode())
    monkeypatch.setattr(android_adb, 'hierarchy', hierarchy)
    with pytest.raises(HTTPException):
        await android.observe(app, actor)
    assert hierarchy.await_count == 0
    await android.control(app, actor, android.ControlRequest(mode='ai', expected_epoch=1))
    xml, meta = await android.observe(app, actor)
    assert xml.startswith(b'<?xml') and meta['mode'] == 'ai'
    assert meta['frame_id'] and meta['width'] == 320
    await android.control(app, actor, android.ControlRequest(mode='human', expected_epoch=meta['epoch']))
    with pytest.raises(HTTPException):
        await android.observe(app, actor)
    assert hierarchy.await_count == 1


@pytest.mark.asyncio
async def test_ui_tree_rejects_entities_and_cleans_device_file(monkeypatch):
    policy = android_adb.AndroidPolicy(enabled=True, adb_binary='/usr/bin/true', serials=['emulator-5554'])
    monkeypatch.setattr(android_adb, 'load_policy', lambda settings: policy)
    runner = AsyncMock(side_effect=[b'ok', b'<!DOCTYPE hierarchy><hierarchy/>', b''])
    monkeypatch.setattr(android_adb, '_run', runner)
    with pytest.raises(HTTPException) as error:
        await android_adb.hierarchy(None, 'emulator-5554')
    assert error.value.status_code == 502
    assert runner.call_args.args[2] == ['shell', 'rm', '-f', '/data/local/tmp/homeai-ui.xml']


@pytest.mark.asyncio
async def test_text_focus_survives_cursor_blink_and_deduplicates(workspace, monkeypatch):
    app, actor, executed = workspace
    signature = AsyncMock(return_value={'fingerprint': 'public-focus', 'password': False})
    monkeypatch.setattr(android_adb, 'focus_signature', signature)
    await android.control(app, actor, android.ControlRequest(mode='human', expected_epoch=1))
    _, frame = await android.capture(app, actor)
    focus = await android.prepare_input(app, actor, android.InputFocusRequest(epoch=frame['epoch']))
    # 光标闪烁改变像素，不改变实际输入目标；文本不应按旧坐标处理。
    monkeypatch.setattr(android_adb, 'screenshot', AsyncMock(return_value=(png(b'cursor'), 320, 640)))
    await android.capture(app, actor)
    request = android.InputRequest(epoch=frame['epoch'], sequence=1, frame_id=frame['frame_id'],
        action='text', text='公开中文', focus_token=focus['focus_token'])
    result = await android.input_event(app, actor, request)
    assert result['status'] == 'succeeded' and executed.await_count == 1
    assert signature.await_count == 2
    assert (await android.input_event(app, actor, request))['status'] == 'succeeded'
    assert executed.await_count == 1
    with app.db() as db:
        claim = db.get(android.AndroidInputFocus, focus['focus_token'])
        assert claim.consumed and claim.fingerprint == 'public-focus'


@pytest.mark.asyncio
async def test_text_changed_field_expired_or_absent_claim_is_rejected(workspace, monkeypatch):
    app, actor, executed = workspace
    signature = AsyncMock(return_value={'fingerprint': 'first-field', 'password': False})
    monkeypatch.setattr(android_adb, 'focus_signature', signature)
    await android.control(app, actor, android.ControlRequest(mode='human', expected_epoch=1))
    _, frame = await android.capture(app, actor)
    request = android.InputRequest(epoch=frame['epoch'], sequence=1, frame_id=frame['frame_id'],
        action='text', text='公开中文')
    with pytest.raises(HTTPException) as absent:
        await android.input_event(app, actor, request)
    assert absent.value.status_code == 409
    focus = await android.prepare_input(app, actor, android.InputFocusRequest(epoch=frame['epoch']))
    request = request.model_copy(update={'focus_token': focus['focus_token']})
    signature.return_value = {'fingerprint': 'different-field', 'password': False}
    with pytest.raises(HTTPException) as moved:
        await android.input_event(app, actor, request)
    assert '输入框或前台窗口已变化' in moved.value.detail
    with app.db() as db:
        claim = db.get(android.AndroidInputFocus, focus['focus_token'])
        claim.expires_at = now() - 1
        db.commit()
    with pytest.raises(HTTPException):
        await android.input_event(app, actor, request)
    assert executed.await_count == 0
    assert (await android.workspace(app, actor))['workspace']['last_sequence'] == 0


@pytest.mark.asyncio
async def test_ai_password_focus_cannot_issue_token(workspace, monkeypatch):
    app, actor, _ = workspace
    monkeypatch.setattr(android_adb, 'focus_signature', AsyncMock(return_value={
        'fingerprint': 'password-field', 'password': True}))
    view = await android.control(app, actor, android.ControlRequest(mode='ai', expected_epoch=1))
    with pytest.raises(HTTPException) as error:
        await android.prepare_input(app, actor, android.InputFocusRequest(epoch=view['workspace']['epoch']), source='ai')
    assert '本人接管' in error.value.detail
    with app.db() as db:
        assert db.scalar(select(android.AndroidInputFocus)) is None


@pytest.mark.asyncio
async def test_focus_fingerprint_ignores_text_but_binds_window_and_control(monkeypatch):
    policy = android_adb.AndroidPolicy(enabled=True, adb_binary='/usr/bin/true', serials=['emulator-5554'])
    monkeypatch.setattr(android_adb, 'load_policy', lambda settings: policy)
    runner = AsyncMock(return_value=b'mCurrentFocus=Window{abc123 u0 com.example.app/.Main}')
    monkeypatch.setattr(android_adb, '_run', runner)
    def tree(text='public', resource='com.example.app:id/search'):
        return ('<hierarchy rotation="0"><node focused="true" enabled="true" password="false" '
            'class="android.widget.EditText" package="com.example.app" bounds="[10,20][300,60]" '
            'resource-id="' + resource + '" text="' + text + '"/></hierarchy>').encode()
    first = await android_adb.focus_signature(None, 'emulator-5554', xml=tree())
    changed_text = await android_adb.focus_signature(None, 'emulator-5554', xml=tree(text='different'))
    assert first == changed_text and 'public' not in str(first)
    other_control = await android_adb.focus_signature(None, 'emulator-5554', xml=tree(resource='com.example.app:id/other'))
    assert first['fingerprint'] != other_control['fingerprint']
    runner.return_value = b'mCurrentFocus=Window{def456 u0 com.example.app/.Other}'
    other_window = await android_adb.focus_signature(None, 'emulator-5554', xml=tree())
    assert first['fingerprint'] != other_window['fingerprint']


@pytest.mark.asyncio
async def test_focus_api_requires_current_human_control(system, alice, workspace, monkeypatch):
    monkeypatch.setattr(android_adb, 'focus_signature', AsyncMock(return_value={
        'fingerprint': 'public-focus', 'password': False}))
    denied = alice.request('POST', '/api/v1/android/input-focus', {'epoch': 1})
    assert denied.status_code == 409
    control = alice.request('POST', '/api/v1/android/control', {'mode': 'human', 'expected_epoch': 1})
    epoch = control.json()['workspace']['epoch']
    response = alice.request('POST', '/api/v1/android/input-focus', {'epoch': epoch})
    assert response.status_code == 200
    assert len(response.json()['focus_token']) == 32
    assert now() < response.json()['expires_at'] <= now() + 30
