"""真人连续触控的指针状态机、取消围栏及进程间排他性。"""
import asyncio
import json
import sys
import time
import uuid
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from homeai import android_workspace as android
from homeai.android_gestures import GestureSession, GesturePacket, gesture_barrier, _paths
from homeai.db import Device, now
from test_android_workspace import workspace, png


class Bridge:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.rotation = 0
        self.width, self.height = 320, 640

    async def send(self, value):
        assert not self.closed
        self.calls.append(value)
        return {'width': self.width, 'height': self.height, 'rotation': self.rotation}

    async def close(self):
        self.calls.append({'phase': 'cancel', 'points': []})
        self.closed = True


@pytest.fixture
async def gesture(workspace):
    app, actor, _ = workspace
    await android.control(app, actor, android.ControlRequest(mode='human', expected_epoch=1))
    _, frame = await android.capture(app, actor)
    bridge = Bridge()
    factory = AsyncMock(return_value=bridge)
    session = GestureSession(app, actor, factory)
    yield app, actor, session, bridge, factory, frame
    await session.close()


def packet(frame, phase='down', sequence=1, gesture_id=None, points=None, changed_id=0):
    result = {'gesture_id': gesture_id or str(uuid.uuid4()), 'epoch': frame['epoch'], 'sequence': sequence,
        'frame_id': frame['frame_id'], 'phase': phase, 'points': points if points is not None else [{'id': 0, 'x': 0.3, 'y': 0.4}]}
    if changed_id is not None:
        result['changed_id'] = changed_id
    return result


@pytest.mark.asyncio
async def test_multi_pointer_long_press_and_up_reuse_bridge(gesture):
    app, actor, session, bridge, factory, frame = gesture
    identifier = str(uuid.uuid4())
    first = [{'id': 0, 'x': 0.3, 'y': 0.4}]
    both = first + [{'id': 1, 'x': 0.7, 'y': 0.4}]
    await session.handle(packet(frame, gesture_id=identifier))
    await session.handle(packet(frame, 'pointer_down', 2, identifier, both, 1))
    await session.handle(packet(frame, 'move', 3, identifier,
        [{'id': 0, 'x': 0.2, 'y': 0.4}, {'id': 1, 'x': 0.8, 'y': 0.4}], None))
    await session.handle(packet(frame, 'keepalive', 4, identifier, [], None))
    await session.handle(packet(frame, 'pointer_up', 5, identifier, both, 1))
    await session.handle(packet(frame, 'up', 6, identifier, first, 0))
    assert session.fd is None and not bridge.closed
    assert [item['phase'] for item in bridge.calls] == ['keepalive', 'down', 'pointer_down', 'move', 'keepalive', 'pointer_up', 'up', 'cancel']
    assert bridge.calls[1]['points'] == [{'id': 0, 'x_px': 96, 'y_px': 256}]
    # 双击沿用仍有效的已发布帧与执行桥。
    await session.handle(packet(frame))
    assert factory.await_count == 1


@pytest.mark.asyncio
async def test_pointer_gap_and_out_of_order_cancel_without_replay(gesture):
    _, _, session, bridge, _, frame = gesture
    identifier = str(uuid.uuid4())
    await session.handle(packet(frame, gesture_id=identifier))
    with pytest.raises(HTTPException):
        await session.handle(packet(frame, 'move', 3, identifier, changed_id=None))
    assert session.fd is None and bridge.calls[-1]['phase'] == 'cancel'
    with pytest.raises(HTTPException):
        await session.handle(packet(frame, gesture_id=identifier))
    assert sum(item['phase'] == 'down' for item in bridge.calls) == 1


@pytest.mark.asyncio
async def test_missing_pointer_and_more_than_five_rejected(gesture):
    _, _, session, bridge, _, frame = gesture
    identifier = str(uuid.uuid4())
    await session.handle(packet(frame, gesture_id=identifier))
    with pytest.raises(HTTPException):
        await session.handle(packet(frame, 'move', 2, identifier, [{'id': 1, 'x': 0.3, 'y': 0.4}], None))
    assert bridge.calls[-1]['phase'] == 'cancel'
    oversized = packet(frame, points=[{'id': value, 'x': 0.2, 'y': 0.3} for value in range(6)])
    with pytest.raises(HTTPException) as error:
        await session.handle(oversized)
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_short_lived_displayed_frame_accepted_but_geometry_and_epoch_checked(gesture, monkeypatch):
    app, actor, session, bridge, _, frame = gesture
    monkeypatch.setattr(android.android_adb, 'screenshot', AsyncMock(return_value=(png(b'newframe'), 320, 640)))
    await android.capture(app, actor)
    await session.handle(packet(frame))
    await session.close()
    another = GestureSession(app, actor, lambda *_: Bridge())
    with app.db() as db:
        row = db.scalar(select(android.AndroidWorkspace))
        row.frame_width = 640
        db.commit()
    with pytest.raises(HTTPException):
        await another.handle(packet(frame))
    assert another.fd is None
    await another.close()


@pytest.mark.asyncio
async def test_human_renewal_preserves_gesture_but_takeover_cancels_before_return(gesture):
    app, actor, session, bridge, _, frame = gesture
    await session.handle(packet(frame))
    await android.control(app, actor, android.ControlRequest(mode='human', expected_epoch=frame['epoch']))
    assert session.fd is not None
    changed = await android.control(app, actor, android.ControlRequest(mode='paused', expected_epoch=frame['epoch']))
    assert changed['workspace']['mode'] == 'paused'
    assert session.fd is None and bridge.calls[-1]['phase'] == 'cancel'


@pytest.mark.asyncio
async def test_legacy_input_cancels_gesture_and_stale_ai_does_not(gesture):
    app, actor, session, bridge, _, frame = gesture
    await session.handle(packet(frame))
    action = android.InputRequest(epoch=frame['epoch'], sequence=1, frame_id=frame['frame_id'], action='home')
    with pytest.raises(HTTPException):
        await android.input_event(app, actor, action, source='ai')
    assert session.fd is not None
    assert (await android.input_event(app, actor, action))['status'] == 'succeeded'
    assert session.fd is None and bridge.calls[-1]['phase'] == 'cancel'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['rotation', 'revocation', 'epoch', 'expiry'])
async def test_watchdog_cancels_on_authority_or_geometry_change(gesture, change):
    app, actor, session, bridge, _, frame = gesture
    await session.handle(packet(frame))
    if change == 'rotation':
        bridge.rotation = 2
    else:
        with app.db() as db:
            if change == 'revocation':
                db.get(Device, actor.device_id).revoked = True
            else:
                row = db.scalar(select(android.AndroidWorkspace))
                if change == 'epoch':
                    row.epoch += 1
                else:
                    row.lease_until = now() - 1
            db.commit()
    for _ in range(15):
        if session.fd is None:
            break
        await asyncio.sleep(0.1)
    assert session.fd is None
    assert bridge.calls[-1]['phase'] == 'cancel'


@pytest.mark.asyncio
async def test_disconnected_client_is_cancelled_and_bridge_closes(gesture):
    _, _, session, bridge, _, frame = gesture
    await session.handle(packet(frame))
    await session.close()
    assert bridge.closed and session.fd is None
    with pytest.raises(HTTPException):
        await session.handle(packet(frame))


@pytest.mark.asyncio
async def test_parallel_channel_cannot_steal_touch(gesture):
    app, actor, session, _, _, frame = gesture
    await session.handle(packet(frame))
    other = GestureSession(app, actor, lambda *_: Bridge())
    with pytest.raises(HTTPException):
        await other.handle(packet(frame))
    assert session.fd is not None and other.fd is None
    await other.close()


@pytest.mark.asyncio
async def test_cross_process_unresponsive_holder_refuses_takeover(gesture):
    app, actor, _, _, _, frame = gesture
    directory = _paths(app, actor)
    script = 'import fcntl,sys,time; f=open(sys.argv[1],"a+"); fcntl.flock(f,fcntl.LOCK_EX); print("ready",flush=True); time.sleep(5)'
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', script, str(directory / 'gesture.lock'),
        stdout=asyncio.subprocess.PIPE)
    try:
        assert await process.stdout.readline() == b'ready\n'
        started = time.monotonic()
        with pytest.raises(HTTPException) as error:
            await android.control(app, actor, android.ControlRequest(mode='paused', expected_epoch=frame['epoch']))
        assert error.value.status_code == 409
        assert time.monotonic() - started < 2.5
        assert (await android.workspace(app, actor))['workspace']['mode'] == 'human'
    finally:
        process.kill()
        await process.wait()


@pytest.mark.asyncio
async def test_orphan_bridge_gets_quiet_period_before_takeover(gesture):
    app, actor, _, _, _, frame = gesture
    directory = _paths(app, actor)
    directory.joinpath('active').write_text(json.dumps({'pid': 0, 'created_at': time.time()}))
    started = time.monotonic()
    async with gesture_barrier(app, actor):
        assert time.monotonic() - started >= 1.5
        assert not directory.joinpath('active').exists()


@pytest.mark.asyncio
async def test_ack_status_and_watchdog_callback(gesture):
    _, _, session, _, _, frame = gesture
    updates = []
    session.on_cancel = updates.append
    first = await session.handle(packet(frame))
    assert first['status'] == 'accepted'
    session.last_client = time.monotonic() - 2
    for _ in range(10):
        if updates:
            break
        await asyncio.sleep(0.1)
    assert updates[-1]['status'] == 'canceled'
    assert updates[-1]['gesture_id'] == first['gesture_id']
    assert updates[-1]['sequence'] == 1
    assert session.fd is None
    identifier = str(uuid.uuid4())
    await session.handle(packet(frame, gesture_id=identifier))
    ended = await session.handle(packet(frame, 'cancel', 2, identifier, [], None))
    assert ended['status'] == 'canceled'


@pytest.mark.asyncio
async def test_binding_revocation_cancels_before_return(system, alice, gesture):
    _, actor, session, bridge, _, frame = gesture
    await session.handle(packet(frame))
    response = await asyncio.to_thread(alice.request, 'DELETE', '/api/v1/manage/android/bindings/' + actor.user_id)
    assert response.status_code == 200
    assert session.fd is None and bridge.calls[-1]['phase'] == 'cancel'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['paused', 'ai', 'human'])
async def test_warm_without_control_or_injection_in_any_mode(workspace, mode):
    from homeai.db import Audit
    app, actor, _ = workspace
    with app.db() as db:
        row = db.scalar(select(android.AndroidWorkspace))
        row.mode = mode
        row.controller_device_id = actor.device_id if mode == 'human' else None
        row.lease_until = now() + 15 if mode == 'human' else 0
        db.commit()
    bridge = Bridge()
    factory = AsyncMock(return_value=bridge)
    session = GestureSession(app, actor, factory)
    try:
        geometry = await session.warm()
        assert geometry == {'width': 320, 'height': 640, 'rotation': 0}
        assert bridge.calls == [{'phase': 'keepalive', 'points': []}]
        assert session.fd is None and session.gesture_id is None
        with app.db() as db:
            assert db.scalar(select(android.AndroidWorkspace)).mode == mode
            assert db.scalar(select(Audit).where(Audit.action.like('android.gesture.%'))) is None
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_warm_reuses_bridge_for_first_gesture_and_rewarms_changed_scope(gesture, monkeypatch):
    app, actor, session, first, factory, frame = gesture
    await session.warm()
    await session.handle(packet(frame))
    assert factory.await_count == 1
    await session._end(None)
    second = Bridge()
    factory.return_value = second
    original = android.android_adb.load_policy(app.settings)
    monkeypatch.setattr(android.android_adb, 'load_policy', lambda settings: original.model_copy(update={'adb_binary': '/usr/bin/false'}))
    await session.warm()
    assert first.closed and session.bridge is second
    assert factory.await_count == 2
    assert second.calls == [{'phase': 'keepalive', 'points': []}]


@pytest.mark.asyncio
async def test_warm_revalidates_member_device_and_local_policy(gesture, monkeypatch):
    app, actor, session, bridge, _, _ = gesture
    policy = android.android_adb.load_policy(app.settings)
    monkeypatch.setattr(android.android_adb, 'load_policy', lambda settings: policy.model_copy(update={'enabled': False}))
    with pytest.raises(HTTPException):
        await session.warm()
    assert bridge.calls == []
    monkeypatch.setattr(android.android_adb, 'load_policy', lambda settings: policy)
    with app.db() as db:
        db.get(Device, actor.device_id).revoked = True
        db.commit()
    with pytest.raises(HTTPException) as error:
        await session.warm()
    assert error.value.status_code == 401
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_gesture_begin_end_audit_contains_only_bounded_metadata(gesture):
    from homeai.db import Audit
    app, _, session, _, _, frame = gesture
    identifier = str(uuid.uuid4())
    await session.warm()
    await session.handle(packet(frame, gesture_id=identifier))
    await session.handle(packet(frame, 'up', 2, identifier))
    await session.close()
    with app.db() as db:
        rows = db.scalars(select(Audit).where(Audit.action.like('android.gesture.%')).order_by(Audit.created_at)).all()
        assert len(rows) == 2
        assert [row.action for row in rows] == ['android.gesture.begin', 'android.gesture.end']
        entries = [json.loads(row.details) for row in rows]
        assert entries == [{'gesture_id': identifier, 'epoch': frame['epoch'], 'event_count': 0, 'status': 'started'},
                           {'gesture_id': identifier, 'epoch': frame['epoch'], 'event_count': 2, 'status': 'completed'}]


@pytest.mark.asyncio
async def test_explicit_cancel_audited_once(gesture):
    from homeai.db import Audit
    app, _, session, _, _, frame = gesture
    identifier = str(uuid.uuid4())
    await session.handle(packet(frame, gesture_id=identifier))
    await session.handle(packet(frame, 'cancel', 2, identifier, [], None))
    await session.close()
    with app.db() as db:
        rows = db.scalars(select(Audit).where(Audit.action == 'android.gesture.end')).all()
        assert len(rows) == 1
        entry = json.loads(rows[0].details)
        assert entry['status'] == 'canceled' and entry['event_count'] == 2


@pytest.mark.asyncio
async def test_disconnect_cancel_keeps_bridge_reusable_after_new_warm(gesture):
    _, _, session, bridge, factory, frame = gesture
    await session.handle(packet(frame))
    await session.cancel('连接中断')
    assert session.fd is None and session.closed is False and bridge.closed is False
    await session.warm()
    assert factory.await_count == 1
    response = await session.handle(packet(frame))
    assert response['status'] == 'accepted'


@pytest.mark.asyncio
async def test_warm_timeout_does_not_claim_ready_or_inject(workspace):
    app, actor, _ = workspace
    cancelled = asyncio.Event()
    async def slow(*_):
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()
    session = GestureSession(app, actor, slow)
    started = time.monotonic()
    with pytest.raises(HTTPException) as error:
        await session.warm()
    assert error.value.status_code == 504
    assert cancelled.is_set() and session.bridge is None and session.fd is None
    assert time.monotonic() - started < 3.5
    await session.close()


@pytest.mark.asyncio
async def test_device_revoked_during_warm_cannot_be_ready(workspace):
    app, actor, _ = workspace
    bridge = Bridge()
    async def factory(*_):
        with app.db() as db:
            db.get(Device, actor.device_id).revoked = True
            db.commit()
        return bridge
    session = GestureSession(app, actor, factory)
    with pytest.raises(HTTPException) as error:
        await session.warm()
    assert error.value.status_code == 401
    assert bridge.closed and all(item['phase'] in ('keepalive', 'cancel') for item in bridge.calls)
    assert session.gesture_id is None
    await session.close()


@pytest.mark.asyncio
async def test_down_ack_with_zero_active_cancels_instead_of_accepting(gesture):
    _, _, session, bridge, _, frame = gesture
    updates = []
    session.on_cancel = updates.append
    original = bridge.send
    async def already_cancelled(value):
        ack = await original(value)
        return {**ack, 'active': 0}
    bridge.send = already_cancelled
    with pytest.raises(HTTPException) as error:
        await session.handle(packet(frame))
    assert error.value.status_code == 409
    assert session.fd is None and session.points == {}
    assert bridge.calls[-1]['phase'] == 'cancel'
    # 首 DOWN 未接受，没有合法的上一条 ACK；由 stream 对当前 seq=1 返回 rejected。
    assert updates == []
    assert session.sequence == 0


@pytest.mark.asyncio
async def test_java_watchdog_active_zero_is_reported_without_next_move(gesture):
    _, _, session, bridge, _, frame = gesture
    updates = []
    session.on_cancel = updates.append
    original = bridge.send
    async def watchdog_cancelled(value):
        ack = await original(value)
        return {**ack, 'active': 1 if value['phase'] == 'down' else 0,
                'cancellations': 1, 'cancel_reason': 'timeout'}
    bridge.send = watchdog_cancelled
    await session.handle(packet(frame))
    assert session.fd is not None
    for _ in range(10):
        if updates:
            break
        await asyncio.sleep(0.1)
    assert session.fd is None and session.points == {}
    assert updates[-1]['status'] == 'canceled'
    assert '触点' in updates[-1]['reason']
    assert not any(item['phase'] == 'move' for item in bridge.calls)


@pytest.mark.asyncio
async def test_cancel_during_down_preparation_never_injects_delayed_down(gesture):
    _, _, session, bridge, _, frame = gesture
    original = bridge.send
    started, release = asyncio.Event(), asyncio.Event()
    async def delayed(value):
        if value['phase'] == 'keepalive' and not started.is_set():
            started.set()
            await release.wait()
        return await original(value)
    bridge.send = delayed
    operation = asyncio.create_task(session.handle(packet(frame)))
    await started.wait()
    cancellation = asyncio.create_task(session.cancel('心跳失活'))
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(HTTPException) as error:
        await operation
    await cancellation
    assert error.value.status_code == 409
    assert not any(item['phase'] == 'down' for item in bridge.calls)
    assert session.fd is None


@pytest.mark.asyncio
async def test_screenshot_workspace_lock_does_not_block_down_but_control_still_cancels_first(gesture, monkeypatch):
    app, actor, session, bridge, _, frame = gesture
    screenshot_started, finish_screenshot = asyncio.Event(), asyncio.Event()
    original = android.android_adb.screenshot
    async def slow_screenshot(*args):
        screenshot_started.set()
        await finish_screenshot.wait()
        return await original(*args)
    monkeypatch.setattr(android.android_adb, 'screenshot', slow_screenshot)
    capture = asyncio.create_task(android.capture(app, actor))
    control = None
    try:
        await screenshot_started.wait()
        # 截图仍持有 workspace._lock，但有效的旧显示帧可立即开始真人手势。
        response = await asyncio.wait_for(session.handle(packet(frame)), 0.5)
        assert response['status'] == 'accepted' and not capture.done()
        assert session.fd is not None
        control = asyncio.create_task(android.control(app, actor,
            android.ControlRequest(mode='paused', expected_epoch=frame['epoch'])))
        for _ in range(10):
            if session.fd is None:
                break
            await asyncio.sleep(0.05)
        # 控制权变更仍必须先完成 CANCEL；不能因为移除截图锁而绕过手势围栏。
        assert session.fd is None and bridge.calls[-1]['phase'] == 'cancel'
        assert not control.done()
        finish_screenshot.set()
        await capture
        assert (await control)['workspace']['mode'] == 'paused'
    finally:
        finish_screenshot.set()
        await asyncio.gather(capture, *([control] if control else []), return_exceptions=True)
