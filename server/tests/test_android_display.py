"""实体屏幕监督、成员权限及退出竞争；仅桥执行使用确定性的故障替身。"""
import asyncio
import hashlib
import json
import time
import uuid
from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from conftest import SignedClient
from test_android_workspace import workspace
from homeai import android_display as display, android_workspace as android
from homeai.db import Device
from homeai.security import Actor


class FakeDisplayBridge:
    def __init__(self):
        self.off = False
        self.supported = True
        self.verified = False
        self.closed = False
        self.actions = []
        self.off_started = asyncio.Event()
        self.off_release = None
        self.off_failure = None
        self.failure_code = None
        self.last = self.response()

    def response(self, *, ok=True, error=''):
        return {'ok': ok, 'supported': self.supported, 'requested_off': self.off,
                'physical_verified': self.verified, 'physical_off': self.off if self.verified else None,
                'restore_pending': self.off, 'error': error}

    async def send(self, action):
        assert action in {'status', 'off', 'on', 'keepalive'}
        assert not self.closed
        self.actions.append(action)
        if action == 'off':
            if self.failure_code:
                self.last = self.response(ok=False, error=self.failure_code)
                return self.last
            self.off = True
            self.off_started.set()
            if self.off_release:
                await self.off_release.wait()
            if self.off_failure:
                raise self.off_failure
        elif action == 'on':
            self.off = False
        self.last = self.response()
        return self.last

    async def close(self):
        if not self.closed:
            await self.send('on')
            self.closed = True


@pytest.fixture
async def display_workspace(workspace, monkeypatch):
    app, actor, _ = workspace
    monkeypatch.setattr(app.settings, 'android_display_control_enabled', True)
    control = await android.control(app, actor, android.ControlRequest(mode='human', expected_epoch=1))
    epoch = control['workspace']['epoch']
    with app.db() as db:
        row = db.scalar(select(android.AndroidWorkspace))
        identifier, serial = row.id, row.serial
    bridge = FakeDisplayBridge()
    async def create(settings, requested_serial):
        assert settings is app.settings and requested_serial == serial
        return bridge
    monkeypatch.setattr(display, 'create_display_bridge', create)
    manager = SimpleNamespace(app=app, entries={})
    guard = display.guard_for(manager, actor, identifier, serial)
    async def attach(stream_id, member=actor):
        manager.entries[stream_id] = {'device_id': member.device_id, 'owner_id': member.user_id,
            'touch_ready': True, 'touch_live_until': time.monotonic() + 120,
            'media': SimpleNamespace(actor=member)}
        return await guard.attach(stream_id)
    await attach('first')
    try:
        yield SimpleNamespace(app=app, actor=actor, epoch=epoch, manager=manager,
            guard=guard, bridge=bridge, attach=attach, workspace_id=identifier, serial=serial)
    finally:
        if bridge.off_release:
            bridge.off_release.set()
        for stream_id in list(guard.streams):
            manager.entries.pop(stream_id, None)
            await guard.detach(stream_id)


def second_device(harness):
    identifier = str(uuid.uuid4())
    with harness.app.db() as db:
        original = db.get(Device, harness.actor.device_id)
        db.add(Device(id=identifier, user_id=harness.actor.user_id, public_key=original.public_key,
            name='另一台已配对测试设备'))
        db.commit()
    return Actor(harness.actor.user_id, harness.actor.household_id, identifier, harness.actor.role)


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['other_member', 'old_epoch', 'not_human', 'not_ready', 'expired', 'revoked'])
async def test_only_live_current_human_device_can_turn_off(system, display_workspace, invalid):
    h = display_workspace
    actor, epoch = h.actor, h.epoch
    if invalid == 'other_member':
        bob = SignedClient(system[1], system[2])
        actor = Actor(bob.user_id, 'h1', bob.device_id, 'adult')
    elif invalid == 'old_epoch':
        epoch -= 1
    elif invalid == 'not_human':
        result = await android.control(h.app, actor, android.ControlRequest(mode='paused', expected_epoch=epoch))
        epoch = result['workspace']['epoch']
    elif invalid == 'not_ready':
        h.manager.entries['first']['touch_ready'] = False
    elif invalid == 'expired':
        h.manager.entries['first']['touch_live_until'] = time.monotonic() - 1
    else:
        with h.app.db() as db:
            db.get(Device, actor.device_id).revoked = True
            db.commit()
    with pytest.raises(HTTPException) as rejected:
        await h.guard.set_off('first', actor, epoch, True)
    assert rejected.value.status_code in {401, 404, 409}
    assert not h.bridge.off and 'off' not in h.bridge.actions


@pytest.mark.asyncio
async def test_off_then_exit_restores_and_reports_unverified_supervised_state(display_workspace):
    h = display_workspace
    first = h.guard.snapshot()
    off = await h.guard.set_off('first', h.actor, h.epoch, True)
    assert off['device_screen_off'] is True
    assert off['display_verified'] is False
    assert '已请求关闭' in off['display_reason']
    assert off['display_revision'] > first['display_revision']
    assert h.guard.owner == 'first' and h.bridge.off
    h.manager.entries.pop('first')
    await h.guard.detach('first')
    assert not h.bridge.off and h.bridge.closed
    assert h.guard.owner is None
    assert h.guard.snapshot()['device_screen_off'] is False
    assert h.guard.snapshot()['display_revision'] > off['display_revision']


@pytest.mark.asyncio
async def test_lost_touch_heartbeat_restores_screen(display_workspace):
    h = display_workspace
    await h.guard.set_off('first', h.actor, h.epoch, True)
    h.manager.entries['first']['touch_live_until'] = time.monotonic() - 1
    h.guard.last_check = 0
    await h.guard.tick()
    assert not h.bridge.off and h.guard.owner is None
    assert h.guard.snapshot()['device_screen_off'] is False


@pytest.mark.asyncio
async def test_another_paired_device_taking_control_restores_old_screen(display_workspace):
    h = display_workspace
    await h.guard.set_off('first', h.actor, h.epoch, True)
    other = second_device(h)
    await h.attach('second', other)
    new = await android.control(h.app, other, android.ControlRequest(mode='human', expected_epoch=h.epoch))
    h.guard.last_check = 0
    await h.guard.tick()
    assert new['workspace']['controller_device_id'] == other.device_id
    assert not h.bridge.off and h.guard.owner is None
    with pytest.raises(HTTPException):
        await h.guard.set_off('first', h.actor, h.epoch, True)


@pytest.mark.asyncio
async def test_old_stream_close_cannot_turn_on_new_owner_screen(display_workspace):
    h = display_workspace
    await h.guard.set_off('first', h.actor, h.epoch, True)
    other = second_device(h)
    await h.attach('second', other)
    new = await android.control(h.app, other, android.ControlRequest(mode='human', expected_epoch=h.epoch))
    await h.guard.set_off('second', other, new['workspace']['epoch'], True)
    assert h.bridge.actions[-2:] == ['on', 'off']
    before = list(h.bridge.actions)
    h.manager.entries.pop('first')
    await h.guard.restore_for('first')
    await h.guard.detach('first')
    assert h.bridge.off and h.guard.owner == 'second' and not h.bridge.closed
    assert h.bridge.actions == before
    assert display.guard_for(h.manager, other, h.workspace_id, h.serial) is h.guard


@pytest.mark.asyncio
async def test_inflight_off_and_close_finish_with_screen_on(display_workspace):
    h = display_workspace
    h.bridge.off_release = asyncio.Event()
    turn_off = asyncio.create_task(h.guard.set_off('first', h.actor, h.epoch, True))
    await asyncio.wait_for(h.bridge.off_started.wait(), 1)
    h.manager.entries.pop('first')  # 真实 ScreenSessions.close_session 先废除入口，再等待 guard 锁。
    closing = asyncio.create_task(h.guard.detach('first'))
    await asyncio.sleep(0)
    assert not closing.done()
    h.bridge.off_release.set()
    result, closed = await asyncio.wait_for(asyncio.gather(turn_off, closing, return_exceptions=True), 2)
    assert isinstance(result, HTTPException) and result.status_code == 404
    assert closed is None
    assert not h.bridge.off and h.bridge.closed and h.guard.owner is None


@pytest.mark.asyncio
async def test_inflight_off_and_device_revocation_restore_screen(display_workspace):
    h = display_workspace
    h.bridge.off_release = asyncio.Event()
    turn_off = asyncio.create_task(h.guard.set_off('first', h.actor, h.epoch, True))
    await asyncio.wait_for(h.bridge.off_started.wait(), 1)
    with h.app.db() as db:
        db.get(Device, h.actor.device_id).revoked = True
        db.commit()
    h.bridge.off_release.set()
    with pytest.raises(HTTPException) as revoked:
        await asyncio.wait_for(turn_off, 2)
    assert revoked.value.status_code == 401
    assert not h.bridge.off and h.guard.owner is None


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['timeout', 'connection_lost', 'cancel'])
async def test_off_result_unknown_must_restore_before_status_keepalive(display_workspace, failure):
    h = display_workspace
    if failure == 'cancel':
        h.bridge.off_release = asyncio.Event()
        operation = asyncio.create_task(h.guard.set_off('first', h.actor, h.epoch, True))
        await asyncio.wait_for(h.bridge.off_started.wait(), 1)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
    else:
        h.bridge.off_failure = asyncio.TimeoutError() if failure == 'timeout' else ConnectionError('测试断连')
        with pytest.raises((asyncio.TimeoutError, ConnectionError, HTTPException)):
            await h.guard.set_off('first', h.actor, h.epoch, True)
    # 命令可能已经生效；不能漏登记恢复责任后由普通状态查询不断续命。
    assert not h.bridge.off
    assert h.guard.owner is None


@pytest.mark.asyncio
async def test_system_lock_failure_keeps_on_and_explains_no_unlock(display_workspace):
    h = display_workspace
    h.bridge.failure_code = 'device_locked'
    with pytest.raises(HTTPException) as blocked:
        await h.guard.set_off('first', h.actor, h.epoch, True)
    assert blocked.value.status_code == 409
    assert not h.bridge.off and h.guard.owner is None
    assert '锁屏' in h.guard.snapshot()['display_reason']


def write_local_display_block(harness, reason):
    path = Path(harness.app.settings.state_dir) / 'android-display-blocked.json'
    key = hashlib.sha256(harness.serial.encode()).hexdigest()
    path.write_text(json.dumps({'devices': {key: reason}}, ensure_ascii=False))
    path.chmod(0o600)
    return path


@pytest.mark.asyncio
async def test_cached_guard_live_block_denies_off_and_clear_advances_revision(display_workspace):
    h = display_workspace
    before = h.guard.snapshot()
    assert before['display_control_supported'] is True
    reason = '已实测该设备无法自动恢复实体屏幕，暂时禁用熄屏'
    path = write_local_display_block(h, reason)
    actions = list(h.bridge.actions)
    # 不先刷新 snapshot：已经缓存的 guard 也必须在执行前重新读取本机禁用记录。
    with pytest.raises(HTTPException) as blocked:
        await h.guard.set_off('first', h.actor, h.epoch, True)
    assert blocked.value.status_code == 409 and blocked.value.detail == reason
    assert not h.bridge.off and h.bridge.actions == actions
    assert display.guard_for(h.manager, h.actor, h.workspace_id, h.serial) is h.guard
    disabled = h.guard.snapshot()
    assert disabled['display_control_supported'] is False
    assert disabled['display_reason'] == reason
    assert disabled['display_revision'] > before['display_revision']
    assert h.guard.snapshot()['display_revision'] == disabled['display_revision']

    # 解除的只是该设备兼容性记录，不能重建 guard 或倒退供 iOS 去重的版本。
    path.write_text(json.dumps({'devices': {}}))
    restored = h.guard.snapshot()
    assert restored['display_control_supported'] is True
    assert restored['display_reason'] == before['display_reason']
    assert restored['display_revision'] > disabled['display_revision']
    assert h.guard.snapshot()['display_revision'] == restored['display_revision']
    assert h.bridge.actions == actions and not h.bridge.off


@pytest.mark.asyncio
async def test_live_compatibility_block_keeps_restore_operation_available(display_workspace):
    h = display_workspace
    await h.guard.set_off('first', h.actor, h.epoch, True)
    assert h.bridge.off and h.guard.owner == 'first'
    reason = '实体屏幕恢复兼容性尚未通过，不再允许熄屏'
    write_local_display_block(h, reason)
    blocked = h.guard.snapshot()
    assert blocked['display_control_supported'] is False
    assert blocked['device_screen_off'] is True
    actions = list(h.bridge.actions)
    with pytest.raises(HTTPException):
        await h.guard.set_off('first', h.actor, h.epoch, True)
    assert h.bridge.actions == actions

    restored = await h.guard.set_off('first', h.actor, h.epoch, False)
    assert not h.bridge.off and h.guard.owner is None
    assert h.bridge.actions == actions + ['on']
    assert restored['device_screen_off'] is False
    assert restored['display_control_supported'] is False
    assert restored['display_reason'] == reason
    assert restored['display_revision'] > blocked['display_revision']


@pytest.mark.asyncio
async def test_disabled_display_never_starts_power_bridge_when_viewing(display_workspace, monkeypatch):
    h = display_workspace
    reason = '用户选择仅无线连接，暂不开启实体熄屏'
    write_local_display_block(h, reason)
    calls = []
    async def forbidden(*args):
        calls.append(args)
        raise AssertionError('禁用熄屏时不能启动电源进程')
    monkeypatch.setattr(display, 'create_display_bridge', forbidden)
    manager = SimpleNamespace(app=h.app, entries={})
    guard = display.guard_for(manager, h.actor, h.workspace_id, h.serial)
    state = await guard.attach('viewer')
    assert state['display_control_supported'] is False and state['display_reason'] == reason
    await guard.tick()
    await guard.detach('viewer')
    assert not calls and guard.bridge is None
