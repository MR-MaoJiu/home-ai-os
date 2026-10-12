"""真实 ICE、DTLS、H264 Packet 和 Opus 传输；仅设备采集替换为公开色块与测试音。"""
import asyncio
import json
import math
import os
import shutil
import struct
import time
import zlib
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import av
import pytest
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.sdp import SessionDescription
from fastapi import HTTPException

from conftest import SignedClient
from homeai import android_adb, android_stream as screen, android_workspace as android
from homeai.db import Device
from homeai.security import Actor


def public_png(width=640, height=360):
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data) & 0xffffffff)
    row = bytes([30, 130, 200]) * width
    pixels = b''.join(b'\x00' + row for _ in range(height))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', width, height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b'')


class PublicMediaCapture:
    def __init__(self, *, audio_enabled=False, sdk=35):
        self.width, self.height, self.sdk = 640, 360, sdk
        self.audio_requested = audio_enabled
        self.audio_available = audio_enabled and sdk >= 30
        self.audio_reason = '' if self.audio_available else ('需要 Android 11 或更高版本' if audio_enabled else '未启用设备声音')
        self.closed, self.video_count, self.audio_count = False, 0, 0
        self.started = time.monotonic()
        self.codec = av.CodecContext.create('libx264', 'w')
        self.codec.width, self.codec.height, self.codec.pix_fmt = self.width, self.height, 'yuv420p'
        self.codec.time_base, self.codec.framerate = Fraction(1, 1_000_000), Fraction(30)
        self.codec.options = {'preset': 'ultrafast', 'tune': 'zerolatency', 'profile': 'baseline',
                             'level': '3.1', 'x264-params': 'keyint=30:scenecut=0:repeat-headers=1'}

    async def video(self):
        await asyncio.sleep(max(0, self.started + self.video_count / 30 - time.monotonic()))
        if self.closed:
            raise HTTPException(409, '公开测试采集已结束')
        frame = av.VideoFrame(self.width, self.height, 'yuv420p')
        for plane, color in zip(frame.planes, (100, 128, 128)):
            plane.update(bytes([color]) * plane.buffer_size)
        frame.pts, frame.time_base = 1_000_000 + self.video_count * 1_000_000 // 30, Fraction(1, 1_000_000)
        packets = self.codec.encode(frame)
        assert len(packets) == 1 and isinstance(packets[0], av.Packet)
        self.video_count += 1
        return packets[0]

    async def audio(self):
        if not self.audio_available:
            raise HTTPException(409, self.audio_reason)
        await asyncio.sleep(max(0, self.started + self.audio_count * 0.02 - time.monotonic()))
        if self.closed:
            raise HTTPException(409, '公开测试采集已结束')
        frame = av.AudioFrame(format='s16', layout='stereo', samples=960)
        first_sample = self.audio_count * 960
        samples = [int(2000 * math.sin(2 * math.pi * 440 * (first_sample + index) / 48000)) for index in range(960)]
        frame.planes[0].update(b''.join(struct.pack('<hh', sample, sample) for sample in samples))
        frame.sample_rate = 48000
        frame.pts, frame.time_base = 1_000_000 + self.audio_count * 20_000, Fraction(1, 1_000_000)
        self.audio_count += 1
        return frame

    async def close(self):
        self.closed = True

    async def check_health(self):
        if self.closed:
            raise HTTPException(503, '公开采集已关闭')
        return True


@pytest.fixture
async def stream_workspace(system, alice, monkeypatch):
    app = system[0].state
    actor = Actor(alice.user_id, 'h1', alice.device_id, 'infrastructure_owner')
    with app.db() as db:
        db.add(android.AndroidWorkspace(owner_id=actor.user_id, household_id='h1', serial='public-video-fixture',
            label='公开屏幕样例', backend='emulator', packages='[]'))
        db.commit()
    app.public_media_controls = SimpleNamespace(sdk=35, enabled=True)
    app.public_media_captures = []
    def policy(settings):
        return android_adb.AndroidPolicy(enabled=app.public_media_controls.enabled,
            adb_binary='/usr/bin/true', serials=['public-video-fixture'])
    async def create_capture(settings, serial, *, audio_enabled=False):
        assert settings is app.settings and serial == 'public-video-fixture'
        capture = PublicMediaCapture(audio_enabled=audio_enabled, sdk=app.public_media_controls.sdk)
        app.public_media_captures.append(capture)
        return capture
    monkeypatch.setattr(android_adb, 'load_policy', policy)
    monkeypatch.setattr(android_adb, 'display_size', AsyncMock(return_value=(640, 360)))
    monkeypatch.setattr(android_adb, 'read_sdk', AsyncMock(return_value=35))
    monkeypatch.setattr('homeai.android_media.create_capture', create_capture)
    monkeypatch.setattr(android, 'capture', AsyncMock(side_effect=AssertionError('连续视频不应退回截图采集')))
    monkeypatch.setattr(screen, 'stun_urls', lambda state: [])
    manager = screen.sessions(app)
    try:
        yield app, actor, manager
    finally:
        await manager.close()


async def receiver_offer(*, audio_enabled=False):
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    peer.addTransceiver('video', direction='recvonly')
    if audio_enabled:
        peer.addTransceiver('audio', direction='recvonly')
    channel = peer.createDataChannel('homeai.android.screen', ordered=True)
    tracks, metadata = asyncio.Queue(), asyncio.Queue()
    @peer.on('track')
    def track_ready(track):
        tracks.put_nowait(track)
    @channel.on('message')
    def metadata_ready(data):
        metadata.put_nowait(json.loads(data))
    try:
        await peer.setLocalDescription(await peer.createOffer())
        return peer, channel, tracks, metadata, screen.StreamOffer(type='offer', sdp=peer.localDescription.sdp, audio_enabled=audio_enabled)
    except BaseException:
        await peer.close()
        raise


async def receive_video(manager, actor):
    peer, channel, tracks, metadata, offer = await receiver_offer()
    identifier = None
    try:
        answer = await manager.negotiate(actor, offer)
        identifier = answer['session_id']
        parsed = SessionDescription.parse(answer['sdp'])
        video = next(item for item in parsed.media if item.kind == 'video')
        assert video.direction == 'sendonly'
        assert video.rtp.codecs[0].mimeType.lower() == 'video/h264'
        assert all(codec.mimeType.lower() in {'video/h264', 'video/rtx'} for codec in video.rtp.codecs)
        assert all(candidate.type != 'relay' for media in parsed.media for candidate in media.ice_candidates)
        await peer.setRemoteDescription(RTCSessionDescription(sdp=answer['sdp'], type='answer'))
        track = await asyncio.wait_for(tracks.get(), 10)
        frame = await asyncio.wait_for(track.recv(), 15)
        data = await asyncio.wait_for(metadata.get(), 5)
        assert frame.width > 0 and frame.height > 0
        assert (frame.width, frame.height) == (data['width'], data['height'])
        assert data['frame_id'] and data['mode'] in ('paused', 'human', 'ai')
        assert peer.connectionState == 'connected' and channel.readyState == 'open'
        return peer, channel, identifier, frame, data
    except BaseException:
        if identifier:
            await manager.close_session(identifier)
        await peer.close()
        raise


@pytest.mark.asyncio
async def test_real_h264_video_metadata_and_close_release_all_resources(stream_workspace):
    app, actor, manager = stream_workspace
    peer, _, identifier, frame, metadata = await receive_video(manager, actor)
    entry = manager.entries[identifier]
    try:
        assert (frame.width, frame.height) == (640, 360)
        assert len(metadata['frame_id']) >= 16
        assert metadata['epoch'] == 1
        assert metadata['video_mode'] == 'continuous' and metadata['target_fps'] == 30
        assert metadata['audio_available'] is False
        assert isinstance(entry['media'].capture, PublicMediaCapture)
        assert app.public_media_captures[0].video_count > 0
        await manager.close_session(identifier)
        await asyncio.sleep(0)
        assert identifier not in manager.entries
        assert entry['peer'].connectionState == 'closed'
        assert entry['track'].readyState == 'ended'
        assert entry['timer'].done()
        assert entry['media'].closed and app.public_media_captures[0].closed
        await manager.close_session(identifier)  # 重复关闭没有重复副作用。
    finally:
        await peer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['relay', 'tcp', 'audio', 'missing_candidates', 'sendrecv'])
async def test_video_offer_rejects_relay_audio_and_incomplete_candidates(stream_workspace, mutation):
    _, actor, manager = stream_workspace
    peer, _, _, _, offer = await receiver_offer()
    try:
        description = SessionDescription.parse(offer.sdp)
        if mutation == 'relay':
            description.media[0].ice_candidates[0].type = 'relay'
        elif mutation == 'tcp':
            description.media[0].ice_candidates[0].protocol = 'tcp'
        elif mutation == 'audio':
            description.media[0].kind = 'audio'
        elif mutation == 'missing_candidates':
            description.media[0].ice_candidates_complete = False
        else:
            description.media[0].direction = 'sendrecv'
        with pytest.raises(HTTPException) as failure:
            await manager.negotiate(actor, screen.StreamOffer(type='offer', sdp=str(description)))
        assert failure.value.status_code == 422
        assert manager.entries == {}
    finally:
        await peer.close()


@pytest.mark.asyncio
async def test_other_member_cannot_watch_or_close_personal_screen(system, stream_workspace):
    app, actor, manager = stream_workspace
    bob = SignedClient(system[1], system[2])
    other = Actor(bob.user_id, 'h1', bob.device_id, 'adult')
    peer, _, _, _, offer = await receiver_offer()
    try:
        with pytest.raises(HTTPException) as unbound:
            await manager.negotiate(other, offer)
        assert unbound.value.status_code == 404
        with pytest.raises(HTTPException) as forged:
            await manager.negotiate(Actor(other.user_id, 'h1', actor.device_id, 'adult'), offer)
        assert forged.value.status_code == 403
        answer = await manager.negotiate(actor, offer)
        identifier = answer['session_id']
        request = SimpleNamespace(app=SimpleNamespace(state=app))
        with pytest.raises(HTTPException) as cannot_close:
            await screen.close(identifier, request, other)
        assert cannot_close.value.status_code == 404 and identifier in manager.entries
        assert await screen.close(identifier, request, actor) == {'closed': True}
        assert identifier not in manager.entries
        with app.db() as db:
            db.get(Device, actor.device_id).revoked = True
            db.commit()
        with pytest.raises(HTTPException) as revoked:
            await manager.negotiate(actor, offer)
        assert revoked.value.status_code == 403
    finally:
        await peer.close()


@pytest.mark.asyncio
async def test_read_only_metadata_channel_cannot_carry_commands(stream_workspace):
    _, actor, manager = stream_workspace
    peer, channel, identifier, _, _ = await receive_video(manager, actor)
    try:
        channel.send('{"action":"tap","x":0.5,"y":0.5}')
        async with asyncio.timeout(5):
            while identifier in manager.entries:
                await asyncio.sleep(.02)
        assert identifier not in manager.entries
    finally:
        await peer.close()


async def receive_audio_video(manager, actor):
    peer, channel, tracks, metadata, offer = await receiver_offer(audio_enabled=True)
    identifier = None
    try:
        answer = await manager.negotiate(actor, offer)
        identifier = answer['session_id']
        parsed = SessionDescription.parse(answer['sdp'])
        audio = next(item for item in parsed.media if item.kind == 'audio')
        assert audio.direction == 'sendonly'
        assert audio.rtp.codecs[0].mimeType.lower() == 'audio/opus'
        assert all(candidate.type != 'relay' for media in parsed.media for candidate in media.ice_candidates)
        await peer.setRemoteDescription(RTCSessionDescription(sdp=answer['sdp'], type='answer'))
        first = await asyncio.wait_for(tracks.get(), 5)
        second = await asyncio.wait_for(tracks.get(), 5)
        received = {first.kind: first, second.kind: second}
        return peer, identifier, received['video'], received['audio'], metadata
    except BaseException:
        if identifier:
            await manager.close_session(identifier)
        await peer.close()
        raise


async def test_continuous_h264_packets_do_not_reencode_or_overwrite_ai_frame(stream_workspace, monkeypatch):
    from aiortc.codecs.h264 import H264Encoder
    app, actor, manager = stream_workspace
    def no_reencode(*args, **kwargs):
        raise AssertionError('H264 Packet 透传不应调用家庭服务端编码器')
    monkeypatch.setattr(H264Encoder, 'encode', no_reencode)
    with app.db() as db:
        row = android.get_binding(app, db, actor)
        row.frame_id, row.frame_digest = 'public-canonical-ai-frame', 'public-canonical-digest'
        row.frame_width, row.frame_height, row.frame_at = 320, 180, 100
        db.commit()
    peer, _, identifier, frame, metadata = await receive_video(manager, actor)
    try:
        video = next(receiver.track for receiver in peer.getReceivers() if receiver.track.kind == 'video')
        following = [await asyncio.wait_for(video.recv(), 2) for _ in range(4)]
        assert all(isinstance(item, av.VideoFrame) for item in following)
        assert [frame.pts] + [item.pts for item in following] == sorted({frame.pts, *(item.pts for item in following)})
        assert app.public_media_captures[0].video_count >= 5
        with app.db() as db:
            row = android.get_binding(app, db, actor)
            assert row.frame_id == 'public-canonical-ai-frame' and row.frame_digest == 'public-canonical-digest'
            assert (row.frame_width, row.frame_height, row.frame_at) == (320, 180, 100)
            recent = json.loads(row.recent_frames)
            assert any(item['id'] == metadata['frame_id'] and item['width'] == 640 for item in recent)
    finally:
        await manager.close_session(identifier)
        await peer.close()


async def test_optional_system_audio_real_opus_and_video_are_received(stream_workspace):
    app, actor, manager = stream_workspace
    peer, identifier, video, audio, metadata = await receive_audio_video(manager, actor)
    entry = manager.entries[identifier]
    try:
        picture, sound = await asyncio.wait_for(asyncio.gather(video.recv(), audio.recv()), 10)
        state = await asyncio.wait_for(metadata.get(), 5)
        assert (picture.width, picture.height) == (640, 360)
        assert isinstance(sound, av.AudioFrame) and sound.sample_rate == 48000
        assert sound.layout.name == 'stereo' and sound.samples > 0
        assert any(bytes(sound.planes[0]))  # 公开测试正弦音经实际 Opus 编解码后仍有声音。
        assert state['audio_available'] is True and state['audio_reason'] == ''
        capture = app.public_media_captures[0]
        assert capture.audio_requested and capture.video_count > 0 and capture.audio_count > 0
    finally:
        await manager.close_session(identifier)
        await peer.close()
    assert entry['audio_track'].readyState == 'ended'
    assert entry['track'].readyState == 'ended' and capture.closed


@pytest.mark.parametrize('direction', ['sendrecv', 'sendonly', 'inactive'])
async def test_audio_offer_rejects_client_upstream_or_inactive_media(stream_workspace, direction):
    _, actor, manager = stream_workspace
    peer, _, _, _, offer = await receiver_offer(audio_enabled=True)
    try:
        description = SessionDescription.parse(offer.sdp)
        next(item for item in description.media if item.kind == 'audio').direction = direction
        with pytest.raises(HTTPException) as error:
            await manager.negotiate(actor, screen.StreamOffer(type='offer', sdp=str(description), audio_enabled=True))
        assert error.value.status_code == 422 and manager.entries == {}
    finally:
        await peer.close()


@pytest.mark.parametrize('requested,present', [(False, True), (True, False)])
async def test_audio_media_section_must_match_explicit_request(stream_workspace, requested, present):
    _, actor, manager = stream_workspace
    peer, _, _, _, offer = await receiver_offer(audio_enabled=present)
    try:
        with pytest.raises(HTTPException) as error:
            await manager.negotiate(actor, offer.model_copy(update={'audio_enabled': requested}))
        assert error.value.status_code == 422 and manager.entries == {}
    finally:
        await peer.close()


async def test_unsupported_audio_reports_reason_and_video_continues(stream_workspace):
    app, actor, manager = stream_workspace
    app.public_media_controls.sdk = 29
    peer, identifier, video, _, metadata = await receive_audio_video(manager, actor)
    try:
        picture = await asyncio.wait_for(video.recv(), 10)
        state = await asyncio.wait_for(metadata.get(), 5)
        assert (picture.width, picture.height) == (640, 360)
        assert state['audio_available'] is False and 'Android 11' in state['audio_reason']
        following = await asyncio.wait_for(video.recv(), 2)
        assert following.pts > picture.pts
        assert app.public_media_captures[0].audio_count == 0
        assert identifier in manager.entries and not app.public_media_captures[0].closed
    finally:
        await manager.close_session(identifier)
        await peer.close()


@pytest.mark.parametrize('revocation', ['device', 'binding', 'local_policy'])
async def test_video_and_audio_stop_after_authorization_revoked(stream_workspace, revocation):
    app, actor, manager = stream_workspace
    peer, identifier, video, audio, _ = await receive_audio_video(manager, actor)
    entry = manager.entries[identifier]
    try:
        await asyncio.wait_for(asyncio.gather(video.recv(), audio.recv()), 10)
        if revocation == 'local_policy':
            app.public_media_controls.enabled = False
        else:
            with app.db() as db:
                if revocation == 'device':
                    db.get(Device, actor.device_id).revoked = True
                else:
                    android.get_binding(app, db, actor).revoked = True
                db.commit()
        async with asyncio.timeout(5):
            while identifier in manager.entries or not app.public_media_captures[0].closed:
                await asyncio.sleep(0.02)
        assert entry['media'].closed
        assert entry['audio_track'].readyState == 'ended' and entry['track'].readyState == 'ended'
        assert app.public_media_captures[0].closed
    finally:
        await manager.close_session(identifier)
        await peer.close()


@pytest.mark.asyncio
async def test_real_android_capture_stream_without_inputs(system, alice, monkeypatch):
    serial = os.getenv('HOMEAI_ANDROID_STREAM_SERIAL')
    if not serial:
        pytest.skip('仅在授权的真实 Android 设备上启用只读屏幕传输验收')
    adb = os.getenv('HOMEAI_ANDROID_ADB') or shutil.which('adb')
    if not adb:
        pytest.fail('真实设备验收需要 ADB 绝对路径')
    app = system[0].state
    actor = Actor(alice.user_id, 'h1', alice.device_id, 'infrastructure_owner')
    path = Path(app.settings.state_dir) / 'android-policy.json'
    path.write_text(json.dumps({'enabled': True, 'adb_binary': str(Path(adb).resolve()), 'serials': [serial]}))
    path.chmod(0o600)
    with app.db() as db:
        db.add(android.AndroidWorkspace(owner_id=actor.user_id, household_id='h1', serial=serial,
            label='授权只读验收设备', backend='physical', packages='[]'))
        db.commit()
    monkeypatch.setattr(screen, 'stun_urls', lambda state: [])
    manager = screen.sessions(app)
    peer = None
    try:
        peer, _, identifier, _, metadata = await receive_video(manager, actor)
        assert metadata['mode'] == 'paused'
        print('ANDROID_STREAM_REAL received_h264=true metadata=true inputs=0')
    finally:
        await manager.close()
        if peer:
            await peer.close()
