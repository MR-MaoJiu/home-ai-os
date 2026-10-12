"""真实 ICE/DTLS/SCTP 触控通道；仅使用公开色块和不连接设备的触控桥替身。"""
import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from fastapi import HTTPException

from conftest import SignedClient
from homeai import android_adb, android_stream as screen, android_workspace as android
from homeai.db import now
from homeai.security import Actor
from test_android_stream import stream_workspace


class PublicBridge:
    def __init__(self, warm_gate=None):
        self.calls = []
        self.closed = False
        self.warm_gate = warm_gate
        self.warm_started = asyncio.Event()

    async def send(self, value):
        assert not self.closed
        self.calls.append(json.loads(json.dumps(value)))
        if len(self.calls) == 1:
            self.warm_started.set()
            if self.warm_gate is not None:
                await self.warm_gate.wait()
        return {'width': 640, 'height': 360, 'rotation': 0}

    async def close(self):
        if not self.closed:
            self.calls.append({'phase': 'cancel', 'points': []})
            self.closed = True


@pytest.fixture
async def touch_workspace(stream_workspace, monkeypatch):
    app, actor, manager = stream_workspace
    policy = android_adb.AndroidPolicy(enabled=True, adb_binary='/usr/bin/true', serials=['public-video-fixture'])
    monkeypatch.setattr(android_adb, 'load_policy', lambda settings: policy)
    bridges, controls = [], SimpleNamespace(warm_gate=None)

    async def create_bridge(settings, serial):
        assert settings is app.settings and serial == 'public-video-fixture'
        bridge = PublicBridge(controls.warm_gate)
        bridges.append(bridge)
        return bridge

    monkeypatch.setattr('homeai.android_touch_bridge.create_bridge', create_bridge)

    with app.db() as db:
        row = android.get_binding(app, db, actor)
        row.mode, row.controller_device_id, row.lease_until = 'human', actor.device_id, now() + 60
        db.commit()
    return app, actor, manager, bridges, controls


async def until(condition, timeout=5):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


async def offer_touch():
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    peer.addTransceiver('video', direction='recvonly')
    metadata_channel = peer.createDataChannel('homeai.android.screen', ordered=True)
    touch_channel = peer.createDataChannel('homeai.android.touch', ordered=True)
    metadata, replies, tracks = asyncio.Queue(), asyncio.Queue(), asyncio.Queue()
    metadata_channel.on('message', lambda value: metadata.put_nowait(json.loads(value)))
    heartbeat = SimpleNamespace(enabled=True, generation=None)
    def touch_reply(value):
        message = json.loads(value)
        if message.get('status') == 'ready':
            heartbeat.generation = message['channel_id']
        if (message.get('status') == 'ping' and heartbeat.enabled and touch_channel.readyState == 'open'
                and message.get('channel_id') == heartbeat.generation):
            touch_channel.send(json.dumps({'type': 'heartbeat', 'channel_id': message['channel_id'], 'nonce': message['nonce']}))
        replies.put_nowait(message)
    touch_channel.on('message', touch_reply)
    peer.on('track', lambda track: tracks.put_nowait(track))
    try:
        await peer.setLocalDescription(await peer.createOffer())
        return SimpleNamespace(peer=peer, metadata_channel=metadata_channel, channel=touch_channel,
            metadata=metadata, replies=replies, tracks=tracks, heartbeat=heartbeat,
            offer=screen.StreamOffer(type='offer', sdp=peer.localDescription.sdp))
    except BaseException:
        await peer.close()
        raise


async def connect(touch_workspace, *, ready=True, heartbeat=True, wait_media=True):
    _, actor, manager, _, _ = touch_workspace
    receiver = await offer_touch()
    receiver.heartbeat.enabled = heartbeat
    identifier = None
    try:
        answer = await manager.negotiate(actor, receiver.offer)
        identifier = answer['session_id']
        receiver.identifier = identifier
        await receiver.peer.setRemoteDescription(RTCSessionDescription(type='answer', sdp=answer['sdp']))
        await until(lambda: receiver.channel.readyState == 'open' and receiver.metadata_channel.readyState == 'open', 10)
        track = await asyncio.wait_for(receiver.tracks.get(), 5)
        receiver.video_track = track
        if not wait_media:
            return receiver
        frame = await asyncio.wait_for(track.recv(), 10)
        receiver.frame = await asyncio.wait_for(receiver.metadata.get(), 5)
        assert (frame.width, frame.height) == (640, 360)
        if ready:
            receiver.ready = await reply(receiver, 'ready')
            await reply(receiver, 'alive')
        return receiver
    except BaseException:
        if identifier:
            await manager.close_session(identifier)
        await receiver.peer.close()
        raise


async def reply(receiver, status, *, gesture_id=None, sequence=None):
    async with asyncio.timeout(5):
        while True:
            value = await receiver.replies.get()
            if value.get('status') == status and (gesture_id is None or value.get('gesture_id') == gesture_id) and (sequence is None or value.get('sequence') == sequence):
                return value


def packet(receiver, *, phase='down', sequence=1, gesture_id=None, points=None, changed_id=0):
    result = {'channel_id': receiver.ready['channel_id'], 'gesture_id': gesture_id or str(uuid.uuid4()),
        'epoch': receiver.frame['epoch'], 'sequence': sequence, 'frame_id': receiver.frame['frame_id'],
        'phase': phase, 'points': points if points is not None else [{'id': 0, 'x': 0.25, 'y': 0.5}]}
    if changed_id is not None:
        result['changed_id'] = changed_id
    return result


async def send(receiver, value, status='accepted'):
    receiver.channel.send(json.dumps(value))
    return await reply(receiver, status, gesture_id=value['gesture_id'], sequence=value['sequence'])


async def close_receiver(manager, receiver):
    await manager.close_session(receiver.identifier)
    await receiver.peer.close()


async def test_touch_handshake_real_sctp_and_multi_pointer_ack(touch_workspace):
    _, _, manager, bridges, _ = touch_workspace
    receiver = await connect(touch_workspace)
    try:
        ready = receiver.ready
        assert ready['protocol'] == 1 and type(ready['protocol']) is int
        assert isinstance(ready['channel_id'], str) and len(ready['channel_id']) == 32
        assert manager.entries[receiver.identifier]['touch_generation'] == ready['channel_id']
        assert receiver.peer.connectionState == 'connected'
        assert len(bridges) == 1 and all(value['phase'] == 'keepalive' for value in bridges[0].calls)
        gesture_id = str(uuid.uuid4())
        first = [{'id': 0, 'x': 0.25, 'y': 0.5}]
        both = first + [{'id': 1, 'x': 0.75, 'y': 0.5}]
        remaining = [both[1]]
        operations = [('down', first, 0), ('pointer_down', both, 1), ('move', both, None),
                      ('pointer_up', both, 0), ('move', remaining, None), ('up', remaining, 1)]
        for sequence, (phase, points, changed_id) in enumerate(operations, 1):
            value = packet(receiver, phase=phase, sequence=sequence, gesture_id=gesture_id,
                           points=points, changed_id=changed_id)
            ack = await send(receiver, value)
            assert ack == {'status': 'accepted', 'gesture_id': gesture_id,
                           'epoch': receiver.frame['epoch'], 'sequence': sequence}
        delivered = [item for item in bridges[0].calls if item['phase'] not in {'keepalive', 'cancel'}]
        assert [item['phase'] for item in delivered] == [item[0] for item in operations]
        assert delivered[0]['points'] == [{'id': 0, 'x_px': 160, 'y_px': 180}]
        assert [point['id'] for point in delivered[3]['points']] == [0, 1]
        assert [point['id'] for point in delivered[4]['points']] == [1]
        gestures = manager.entries[receiver.identifier]['gestures']
        assert gestures.fd is None and gestures.gesture_id is None
    finally:
        await close_receiver(manager, receiver)


async def test_not_warmed_missing_and_wrong_channel_ids_never_inject(touch_workspace):
    _, _, manager, bridges, controls = touch_workspace
    controls.warm_gate = asyncio.Event()
    receiver = await connect(touch_workspace, ready=False, wait_media=False)
    try:
        await until(lambda: bool(bridges) and bridges[0].warm_started.is_set())
        assert not manager.entries[receiver.identifier]['touch_ready']
        receiver.ready = {'channel_id': 'not-issued-before-warm'}
        # 预热未完成时没有正式画面凭据；此包必须在连接鉴权层直接拒绝。
        before = {'channel_id': receiver.ready['channel_id'], 'gesture_id': str(uuid.uuid4()),
            'epoch': 1, 'sequence': 1, 'phase': 'down', 'frame_id': 'unpublished-public-test-frame',
            'changed_id': 0, 'points': [{'id': 0, 'x': 0.25, 'y': 0.5}]}
        receiver.channel.send(json.dumps(before))
        await asyncio.sleep(0.05)
        assert all(item['phase'] == 'keepalive' for item in bridges[0].calls)
        # 预热前输入会明确拒绝，但不会派发到桥。
        assert all(item['phase'] == 'keepalive' for item in bridges[0].calls)
        controls.warm_gate.set()
        await reply(receiver, 'rejected', gesture_id=before['gesture_id'], sequence=1)
        receiver.ready = await reply(receiver, 'ready')
        await reply(receiver, 'alive')
        rendered = await asyncio.wait_for(receiver.video_track.recv(), 10)
        receiver.frame = await asyncio.wait_for(receiver.metadata.get(), 5)
        assert (rendered.width, rendered.height) == (640, 360)
        missing = packet(receiver)
        missing.pop('channel_id')
        await send(receiver, missing, 'rejected')
        wrong = {**packet(receiver), 'channel_id': 'expired-public-channel-id'}
        await send(receiver, wrong, 'rejected')
        assert all(item['phase'] == 'keepalive' for item in bridges[0].calls)
        assert receiver.channel.readyState == 'open'
        await send(receiver, packet(receiver))
        assert sum(item['phase'] == 'down' for item in bridges[0].calls) == 1
    finally:
        controls.warm_gate.set()
        await close_receiver(manager, receiver)


async def test_old_connection_generation_down_does_not_cancel_new_gesture(touch_workspace):
    _, _, manager, bridges, _ = touch_workspace
    receiver = await connect(touch_workspace)
    try:
        previous = receiver.ready['channel_id']
        entry = manager.entries[receiver.identifier]
        # 模拟 ICE 恢复时的连接代次切换；消息仍经过真实 SCTP 通道。
        entry['touch_ready'], entry['touch_generation'] = False, None
        await entry['gestures'].cancel('公开测试连接恢复')
        assert await manager.warm_touch(receiver.identifier) is True
        receiver.ready = await reply(receiver, 'ready')
        await reply(receiver, 'alive')
        assert receiver.ready['channel_id'] != previous
        current = packet(receiver)
        await send(receiver, current)
        cancels_before = sum(item['phase'] == 'cancel' for item in bridges[0].calls)
        stale = {**packet(receiver), 'channel_id': previous}
        await send(receiver, stale, 'rejected')
        assert sum(item['phase'] == 'cancel' for item in bridges[0].calls) == cancels_before
        assert entry['gestures'].gesture_id == current['gesture_id']
        await send(receiver, packet(receiver, phase='move', sequence=2,
            gesture_id=current['gesture_id'], changed_id=None))
        await send(receiver, packet(receiver, phase='up', sequence=3,
            gesture_id=current['gesture_id']))
        assert sum(item['phase'] == 'down' for item in bridges[0].calls) == 1
    finally:
        await close_receiver(manager, receiver)


async def test_touch_channel_close_releases_input_but_keeps_video(touch_workspace):
    _, _, manager, bridges, _ = touch_workspace
    receiver = await connect(touch_workspace)
    entry = manager.entries[receiver.identifier]
    try:
        await send(receiver, packet(receiver))
        assert entry['gestures'].fd is not None
        receiver.channel.close()
        await until(lambda: bridges[0].closed and entry['touch_reader'].done())
        assert manager.entries[receiver.identifier] is entry
        assert entry['peer'].connectionState == 'connected'
        assert any(item['phase'] == 'cancel' for item in bridges[0].calls)
        assert entry['gestures'].closed and entry['gestures'].fd is None
        assert entry['gestures'].gesture_id is None and entry['gestures'].points == {}
        assert not entry['gestures'].directory.joinpath('active').exists()
        assert entry['track'].readyState == 'live' and not entry['media'].closed
    finally:
        await close_receiver(manager, receiver)


async def test_other_member_cannot_open_or_close_touch_session(system, touch_workspace):
    app, actor, manager, bridges, _ = touch_workspace
    bob = SignedClient(system[1], system[2])
    other = Actor(bob.user_id, 'h1', bob.device_id, 'adult')
    receiver = await offer_touch()
    try:
        with pytest.raises(HTTPException) as unbound:
            await manager.negotiate(other, receiver.offer)
        assert unbound.value.status_code == 404
        with pytest.raises(HTTPException) as forged:
            await manager.negotiate(Actor(other.user_id, 'h1', actor.device_id, 'adult'), receiver.offer)
        assert forged.value.status_code == 403
        assert manager.entries == {} and bridges == []
    finally:
        await receiver.peer.close()
    owner = await connect(touch_workspace)
    try:
        request = SimpleNamespace(app=SimpleNamespace(state=app))
        with pytest.raises(HTTPException) as denied:
            await screen.close(owner.identifier, request, other)
        assert denied.value.status_code == 404
        assert owner.identifier in manager.entries and owner.channel.readyState == 'open'
        assert not bridges[0].closed
    finally:
        await close_receiver(manager, owner)


async def test_idle_heartbeat_expiry_fences_late_down_without_ice_disconnect(touch_workspace):
    _, _, manager, bridges, _ = touch_workspace
    receiver = await connect(touch_workspace)
    try:
        previous = receiver.ready['channel_id']
        receiver.heartbeat.enabled = False
        refreshed = await reply(receiver, 'ready')
        assert receiver.peer.connectionState == 'connected'
        assert refreshed['channel_id'] != previous
        late = {**packet(receiver), 'channel_id': previous}
        await send(receiver, late, 'rejected')
        assert not any(item['phase'] == 'down' for item in bridges[0].calls)
        receiver.ready = refreshed
        receiver.heartbeat.enabled = True
        await reply(receiver, 'alive')
        await send(receiver, packet(receiver))
        assert sum(item['phase'] == 'down' for item in bridges[0].calls) == 1
    finally:
        await close_receiver(manager, receiver)


async def test_first_down_requires_confirmed_heartbeat(touch_workspace):
    _, _, manager, bridges, _ = touch_workspace
    receiver = await connect(touch_workspace, ready=False, heartbeat=False)
    try:
        receiver.ready = await reply(receiver, 'ready')
        assert receiver.ready['heartbeat_required'] is True
        await send(receiver, packet(receiver), 'rejected')
        assert not any(item['phase'] == 'down' for item in bridges[0].calls)
        receiver.heartbeat.enabled = True
        await reply(receiver, 'alive')
        await send(receiver, packet(receiver))
        assert sum(item['phase'] == 'down' for item in bridges[0].calls) == 1
    finally:
        await close_receiver(manager, receiver)


async def test_cancel_callback_invalidates_generation_and_discards_pending_down(touch_workspace):
    _, _, manager, bridges, _ = touch_workspace
    receiver = await connect(touch_workspace)
    try:
        first = packet(receiver)
        await send(receiver, first)
        entry = manager.entries[receiver.identifier]
        previous = receiver.ready['channel_id']
        queued = packet(receiver)
        entry['touch_queue'].put_nowait(json.dumps(queued))
        await entry['gestures'].cancel('公开测试触点看门狗超时')
        assert entry['touch_queue'].empty()
        assert entry['touch_ready'] is False and entry['touch_generation'] is None
        await reply(receiver, 'canceled', gesture_id=first['gesture_id'])
        receiver.ready = await reply(receiver, 'ready')
        await reply(receiver, 'alive')
        assert receiver.ready['channel_id'] != previous
        await send(receiver, {**packet(receiver), 'channel_id': previous}, 'rejected')
        assert sum(item['phase'] == 'down' for item in bridges[0].calls) == 1
        assert receiver.peer.connectionState == 'connected'
    finally:
        await close_receiver(manager, receiver)


async def test_expired_and_replayed_heartbeat_cannot_renew_old_generation(touch_workspace):
    import time
    _, _, manager, _, _ = touch_workspace
    receiver = await connect(touch_workspace)
    try:
        receiver.heartbeat.enabled = False
        entry = manager.entries[receiver.identifier]
        current = receiver.ready['channel_id']
        nonce = uuid.uuid4().hex
        entry['touch_challenges'][nonce] = time.monotonic() - 0.01
        before = entry['touch_live_until']
        assert manager.confirm_touch_heartbeat(receiver.identifier, {
            'type': 'heartbeat', 'channel_id': current, 'nonce': nonce}) is False
        assert entry['touch_live_until'] == before
        entry['touch_challenges'][nonce] = time.monotonic() + 1
        assert manager.confirm_touch_heartbeat(receiver.identifier, {
            'type': 'heartbeat', 'channel_id': current, 'nonce': nonce}) is True
        assert manager.confirm_touch_heartbeat(receiver.identifier, {
            'type': 'heartbeat', 'channel_id': current, 'nonce': nonce}) is False
        # 即便某个旧挑战尚未到期，连接确认期限已过也不能靠迟到 pong 复活。
        entry['touch_live_until'] = time.monotonic() - 0.01
        other = uuid.uuid4().hex
        entry['touch_challenges'][other] = time.monotonic() + 1
        assert manager.confirm_touch_heartbeat(receiver.identifier, {
            'type': 'heartbeat', 'channel_id': current, 'nonce': other}) is False
        assert entry['touch_generation'] is None
    finally:
        await close_receiver(manager, receiver)
