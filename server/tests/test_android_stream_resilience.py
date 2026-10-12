"""公开静止画面的真实 WebRTC 回归；不连接 Android，不修改其他媒体测试夹具。"""
import asyncio
import json
import struct
import time
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import AsyncMock

import av
import pytest
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from fastapi import HTTPException

from homeai import android_adb, android_media, android_stream as screen, android_workspace as android
from homeai.db import Device
from homeai.security import Actor


class PublicIdleCapture:
    def __init__(self, settings, serial):
        self.width, self.height, self.sdk = 640, 360, 35
        self.audio_available, self.audio_reason = False, '未启用设备声音'
        self.closed, self.eof, self.healthy = False, False, True
        self.video_count, self.produced_count, self.health_count, self.budget = 0, 0, 0, 4
        self.changed = asyncio.Event()
        self.process = SimpleNamespace(returncode=None)
        self.video_error = None
        self.started = time.monotonic()
        self.codec = av.CodecContext.create('libx264', 'w')
        self.codec.width, self.codec.height, self.codec.pix_fmt = self.width, self.height, 'yuv420p'
        self.codec.time_base, self.codec.framerate = Fraction(1, 1_000_000), Fraction(30)
        self.codec.options = {'preset': 'ultrafast', 'tune': 'zerolatency', 'profile': 'baseline',
                             'level': '3.1', 'x264-params': 'keyint=1:scenecut=0:repeat-headers=1'}
        # 公开编码器代替硬件，字节仍经过生产 scrcpy 帧头解析与视频队列。
        self.decoder = android_media.AndroidMediaCapture(settings, serial)
        self.decoder.video_reader = asyncio.StreamReader()
        self.decoder.video_reader.feed_data(struct.pack('>III', 0x80000000, self.width, self.height))
        self.parser = asyncio.create_task(self.decoder._read_video())
        self.decoder.tasks.append(self.parser)
        self.producer = asyncio.create_task(self._produce())

    def resume_frames(self, count=4):
        self.budget += count
        self.changed.set()

    def end_stream(self):
        self.eof = True
        self.decoder.video_reader.feed_eof()
        self.changed.set()

    async def _produce(self):
        while not self.closed and not self.eof:
            while self.produced_count >= self.budget and not self.closed and not self.eof:
                self.changed.clear()
                await self.changed.wait()
            if self.closed or self.eof:
                return
            await asyncio.sleep(1 / 30)
            frame = av.VideoFrame(self.width, self.height, 'yuv420p')
            for plane, color in zip(frame.planes, (100, 128, 128)):
                plane.update(bytes([color]) * plane.buffer_size)
            frame.pts = int((time.monotonic() - self.started + 1) * 1_000_000)
            frame.time_base = Fraction(1, 1_000_000)
            packets = self.codec.encode(frame)
            assert len(packets) == 1
            packet = packets[0]
            data = bytes(packet)
            self.decoder.video_reader.feed_data(struct.pack('>QI', android_media.FLAG_KEYFRAME | packet.pts, len(data)) + data)
            self.produced_count += 1

    async def video(self):
        packet = await self.decoder.video()
        self.video_count += 1
        return packet

    async def check_health(self):
        self.health_count += 1
        if self.closed or not self.healthy or self.process.returncode is not None or self.decoder.video_error:
            raise HTTPException(503, '公开测试采集器健康检查失败')

    async def close(self):
        self.closed = True
        self.changed.set()
        self.producer.cancel()
        await asyncio.gather(self.producer, return_exceptions=True)
        await self.decoder.close()


class PublicTouchBridge:
    def __init__(self):
        self.closed = False
        self.calls = []

    async def send(self, value):
        assert not self.closed
        self.calls.append(value)
        return {'width': 640, 'height': 360, 'rotation': 0, 'active': 0, 'cancellations': 0}

    async def close(self):
        self.calls.append({'phase': 'cancel', 'points': []})
        self.closed = True


@pytest.fixture
async def resilient_media(system, alice, monkeypatch):
    app = system[0].state
    actor = Actor(alice.user_id, 'h1', alice.device_id, 'infrastructure_owner')
    serial = 'public-resilience-device'
    with app.db() as db:
        db.add(android.AndroidWorkspace(owner_id=actor.user_id, household_id='h1', serial=serial,
            label='公开静止画面', backend='emulator', packages='[]'))
        db.commit()
    policy = android_adb.AndroidPolicy(enabled=True, adb_binary='/usr/bin/true', serials=[serial])
    monkeypatch.setattr(android_adb, 'load_policy', lambda settings: policy)
    monkeypatch.setattr(android_adb, 'display_size', AsyncMock(return_value=(640, 360)))
    monkeypatch.setattr(android_adb, 'read_sdk', AsyncMock(return_value=35))
    monkeypatch.setattr(android, 'capture', AsyncMock(side_effect=AssertionError('不能用截图代替连续媒体')))
    capture = PublicIdleCapture(app.settings, serial)
    bridge = PublicTouchBridge()
    async def factory(settings, identifier, *, audio_enabled=False):
        assert settings is app.settings and identifier == serial and not audio_enabled
        return capture
    monkeypatch.setattr('homeai.android_media.create_capture', factory)
    monkeypatch.setattr('homeai.android_touch_bridge.create_bridge', AsyncMock(return_value=bridge))
    monkeypatch.setattr(screen, 'stun_urls', lambda state: [])
    manager = screen.sessions(app)
    try:
        yield app, actor, manager, capture, bridge
    finally:
        await manager.close()
        await capture.close()


async def until(condition, timeout=5):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.02)


async def connect(environment, *, touch=False):
    _, actor, manager, _, _ = environment
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    peer.addTransceiver('video', direction='recvonly')
    channel = peer.createDataChannel('homeai.android.screen', ordered=True)
    frames, metadata, touches = asyncio.Queue(), asyncio.Queue(), asyncio.Queue()
    heartbeat = SimpleNamespace(enabled=True, generation=None)
    peer.on('track', lambda value: frames.put_nowait(value))
    channel.on('message', lambda value: metadata.put_nowait(json.loads(value)))
    touch_channel = peer.createDataChannel('homeai.android.touch', ordered=True) if touch else None
    if touch_channel:
        def touch_message(value):
            message = json.loads(value)
            if message.get('status') == 'ready':
                heartbeat.generation = message['channel_id']
            if (message.get('status') == 'ping' and heartbeat.enabled and touch_channel.readyState == 'open'
                    and message.get('channel_id') == heartbeat.generation):
                touch_channel.send(json.dumps({'type': 'heartbeat', 'channel_id': message['channel_id'], 'nonce': message['nonce']}))
            touches.put_nowait(message)
        touch_channel.on('message', touch_message)
    identifier = None
    try:
        await peer.setLocalDescription(await peer.createOffer())
        answer = await manager.negotiate(actor, screen.StreamOffer(type='offer', sdp=peer.localDescription.sdp))
        identifier = answer['session_id']
        await peer.setRemoteDescription(RTCSessionDescription(type='answer', sdp=answer['sdp']))
        track = await asyncio.wait_for(frames.get(), 5)
        first = await asyncio.wait_for(track.recv(), 10)
        meta = await asyncio.wait_for(metadata.get(), 5)
        assert (first.width, first.height) == (640, 360) and meta['frame_id']
        return SimpleNamespace(peer=peer, channel=channel, track=track, first=first, metadata=metadata,
            identifier=identifier, touches=touches, touch_channel=touch_channel, heartbeat=heartbeat)
    except BaseException:
        if identifier:
            await manager.close_session(identifier)
        await peer.close()
        raise


async def close(environment, receiver):
    await environment[2].close_session(receiver.identifier)
    await receiver.peer.close()


async def test_static_video_survives_twelve_seconds_with_live_metadata_and_resumes(resilient_media):
    _, _, manager, capture, _ = resilient_media
    receiver = await connect(resilient_media)
    try:
        await until(lambda: capture.video_count == 4)
        count = capture.video_count
        await asyncio.sleep(12.2)  # 同时跨过旧的 3 秒视频判死和 10 秒读头超时窗口。
        assert capture.video_count == count and not capture.closed
        assert receiver.identifier in manager.entries and receiver.peer.connectionState == 'connected'
        assert receiver.channel.readyState == 'open' and capture.health_count >= 2
        updates = []
        while not receiver.metadata.empty():
            updates.append(receiver.metadata.get_nowait())
        assert len(updates) >= 10 and all('blocked_reason' not in value for value in updates)
        assert all(value['video_mode'] == 'continuous' for value in updates)
        capture.resume_frames()
        async with asyncio.timeout(5):
            while True:
                frame = await receiver.track.recv()
                if frame.pts > 10 * 90000:
                    break
        assert not capture.closed and receiver.identifier in manager.entries
    finally:
        await close(resilient_media, receiver)


@pytest.mark.parametrize('fault', ['revoked', 'eof', 'unhealthy'])
async def test_idle_stream_still_closes_for_real_authorization_or_capture_failures(resilient_media, fault):
    app, actor, manager, capture, _ = resilient_media
    receiver = await connect(resilient_media)
    try:
        await until(lambda: capture.video_count == 4)
        if fault == 'revoked':
            with app.db() as db:
                db.get(Device, actor.device_id).revoked = True
                db.commit()
        elif fault == 'eof':
            capture.end_stream()
        else:
            capture.healthy = False
        await until(lambda: receiver.identifier not in manager.entries and capture.closed, 6)
        assert capture.closed
    finally:
        await close(resilient_media, receiver)


async def test_metadata_publication_lock_does_not_block_h264_packet_delivery(resilient_media):
    _, _, manager, capture, _ = resilient_media
    receiver = await connect(resilient_media)
    media = manager.entries[receiver.identifier]['media']
    locked = False
    try:
        await media.metadata_lock.acquire()
        locked = True
        capture.resume_frames(120)
        await asyncio.sleep(1.8)
        count = capture.video_count
        await until(lambda: capture.video_count >= count + 3, 1)
        assert receiver.identifier in manager.entries and not capture.closed
    finally:
        if locked:
            media.metadata_lock.release()
        await close(resilient_media, receiver)


async def test_touch_heartbeat_loss_retires_inputs_without_closing_video(resilient_media):
    _, _, manager, capture, _ = resilient_media
    receiver = await connect(resilient_media, touch=True)
    try:
        entry = manager.entries[receiver.identifier]
        await until(lambda: entry['touch_live_until'] > time.monotonic())
        previous = entry['touch_generation']
        receiver.heartbeat.enabled = False
        await until(lambda: entry['touch_generation'] != previous, 4)
        assert receiver.identifier in manager.entries and not capture.closed
        assert receiver.peer.connectionState == 'connected' and receiver.channel.readyState == 'open'
        capture.resume_frames()
        assert (await asyncio.wait_for(receiver.track.recv(), 3)).width == 640
    finally:
        await close(resilient_media, receiver)


async def test_touch_channel_closure_preserves_read_only_video(resilient_media):
    _, _, manager, capture, bridge = resilient_media
    receiver = await connect(resilient_media, touch=True)
    try:
        entry = manager.entries[receiver.identifier]
        await until(lambda: entry['touch_ready'])
        receiver.touch_channel.close()
        await until(lambda: bridge.closed)
        assert receiver.identifier in manager.entries and not capture.closed
        assert receiver.peer.connectionState == 'connected' and receiver.channel.readyState == 'open'
        capture.resume_frames()
        assert (await asyncio.wait_for(receiver.track.recv(), 3)).width == 640
    finally:
        await close(resilient_media, receiver)


async def test_same_peer_replaces_touch_channel_after_delayed_bridge_cleanup(resilient_media, monkeypatch):
    _, _, manager, capture, _ = resilient_media
    closing = asyncio.Event()
    timeline = {}
    bridges = []

    class DelayedBridge(PublicTouchBridge):
        async def close(self):
            if self.closed:
                return
            closing.set()
            timeline['close_started'] = time.monotonic()
            await asyncio.sleep(0.2)
            await super().close()
            timeline['close_finished'] = time.monotonic()

    async def create_bridge(*args, **kwargs):
        if bridges:
            assert bridges[0].closed  # 新桥不能抢在旧桥取消和资源释放之前启动。
        bridge = DelayedBridge() if not bridges else PublicTouchBridge()
        bridges.append(bridge)
        return bridge

    monkeypatch.setattr('homeai.android_touch_bridge.create_bridge', create_bridge)
    receiver = await connect(resilient_media, touch=True)
    try:
        entry = manager.entries[receiver.identifier]
        server_peer = entry['peer']
        client_peer = receiver.peer
        await until(lambda: entry['touch_live_until'] > time.monotonic())
        previous_generation = entry['touch_generation']
        original_media = entry['media']
        count_before = capture.video_count
        capture.resume_frames(60)

        receiver.touch_channel.close()
        replacement = client_peer.createDataChannel('homeai.android.touch', ordered=True)
        received = asyncio.Queue()
        generation = {'value': None}

        def message(value):
            data = json.loads(value)
            if data.get('status') == 'ready':
                generation['value'] = data['channel_id']
                timeline['new_ready'] = time.monotonic()
            elif (data.get('status') == 'ping' and replacement.readyState == 'open'
                  and data.get('channel_id') == generation['value']):
                replacement.send(json.dumps({'type': 'heartbeat', 'channel_id': data['channel_id'], 'nonce': data['nonce']}))
            received.put_nowait(data)

        replacement.on('message', message)
        await asyncio.wait_for(closing.wait(), 3)
        await until(lambda: entry.get('touch_pending_channel') is not None, 1)
        assert entry['touch_closing'] and not bridges[0].closed
        assert len(bridges) == 1 and 'new_ready' not in timeline
        assert receiver.identifier in manager.entries and client_peer.connectionState == 'connected'

        async with asyncio.timeout(5):
            while True:
                ack = await received.get()
                if ack.get('status') == 'alive' and ack.get('channel_id') == generation['value']:
                    break
        assert generation['value'] and generation['value'] != previous_generation
        assert timeline['new_ready'] >= timeline['close_finished']
        assert len(bridges) == 2 and bridges[0].closed and not bridges[1].closed
        assert entry.get('touch_pending_channel') is None and not entry['touch_closing']
        assert entry['touch_live_until'] > time.monotonic()
        assert manager.entries[receiver.identifier] is entry
        assert entry['peer'] is server_peer and receiver.peer is client_peer
        assert entry['media'] is original_media and not capture.closed
        assert receiver.channel.readyState == 'open' and replacement.readyState == 'open'
        await until(lambda: capture.video_count > count_before + 3)
        assert (await asyncio.wait_for(receiver.track.recv(), 3)).width == 640
    finally:
        await close(resilient_media, receiver)
