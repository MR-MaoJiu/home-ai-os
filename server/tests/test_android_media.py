"""连续媒体协议、编码直通和资源边界；只使用公开协议包与公开 PCM。"""
import asyncio
import hashlib
import struct
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import AsyncMock

import av
import pytest
from fastapi import HTTPException
from homeai import android_media as media

SPS_PPS = b'\x00\x00\x00\x01\x67\x42\xe0\x1f\x80\x00\x00\x00\x01\x68\x80'
IDR = b'\x00\x00\x00\x01\x65\x80public'
DELTA = b'\x00\x00\x00\x01\x41\x80public'


def session(width=1280, height=720, resize=False):
    return struct.pack('>III', 0x80000000 | int(resize), width, height)


def packet(payload, pts=1000000, *, config=False, keyframe=False):
    flags = media.FLAG_CONFIG if config else pts | (media.FLAG_KEYFRAME if keyframe else 0)
    return struct.pack('>QI', flags, len(payload)) + payload


@pytest.fixture
async def capture(tmp_path):
    value = media.AndroidMediaCapture(SimpleNamespace(state_dir=tmp_path), 'public-serial')
    yield value
    await value.close()


def test_fixed_capture_arguments_disable_control_clipboard_and_microphone():
    values = media.server_arguments('1234abcd', audio_enabled=True)
    assert values[0] == '4.1'
    for option in ('audio_source=output', 'audio_codec=raw', 'video_codec=h264', 'control=false',
                   'clipboard_autosync=false', 'send_stream_meta=true', 'send_dummy_byte=true',
                   'video_codec_options=i-frame-interval=1,profile=1,level=512,frame-rate=30', 'cleanup=false', 'power_on=false'):
        assert option in values
    assert not any('mic' in value or 'raw_stream=' in value or 'send_codec_meta=' in value for value in values)
    with pytest.raises(ValueError):
        media.server_arguments('1234;id', audio_enabled=True)


def test_pinned_component_rejects_changed_bytes_writable_mode_and_symlinks(tmp_path, monkeypatch):
    settings = SimpleNamespace(state_dir=tmp_path)
    target = media.server_path(settings)
    target.parent.mkdir()
    public = b'public official artifact checksum fixture'
    monkeypatch.setattr(media, 'SCRCPY_SHA256', hashlib.sha256(public).hexdigest())
    target.write_bytes(public)
    target.chmod(0o600)
    assert media.checked_server(settings) == target
    target.write_bytes(public + b'tampered')
    with pytest.raises(HTTPException): media.checked_server(settings)
    target.write_bytes(public)
    target.chmod(0o666)
    with pytest.raises(HTTPException): media.checked_server(settings)
    target.unlink()
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.write_bytes(public)
    target.symlink_to(elsewhere)
    with pytest.raises(HTTPException): media.checked_server(settings)


@pytest.mark.asyncio
async def test_h264_video_is_packet_passthrough_with_shared_source_clock(capture):
    reader = capture.video_reader = asyncio.StreamReader()
    task = asyncio.create_task(capture._read_video())
    capture.tasks.append(task)
    timestamp = 987654321000
    reader.feed_data(session() + packet(SPS_PPS, config=True) + packet(IDR, timestamp, keyframe=True))
    result = await asyncio.wait_for(capture.video(), 1)
    assert isinstance(result, av.Packet)
    assert bytes(result) == SPS_PPS + IDR
    assert result.pts == result.dts == timestamp
    assert result.time_base == Fraction(1, 1000000)
    assert result.is_keyframe and (capture.width, capture.height) == (1280, 720)
    reader.feed_data(packet(DELTA, timestamp + 33333))
    following = await asyncio.wait_for(capture.video(), 1)
    assert bytes(following) == DELTA and not following.is_keyframe


@pytest.mark.asyncio
async def test_h264_queue_overload_discards_delta_until_keyframe(capture):
    capture._push_video(SPS_PPS + IDR, 0, True)
    for index in range(1, 9):
        capture._push_video(DELTA, index, False)
    assert capture.video_queue.empty() and capture.waiting_keyframe
    capture._push_video(DELTA, 10, False)
    assert capture.video_queue.empty()
    capture._push_video(SPS_PPS + IDR, 11, True)
    result = await capture.video()
    assert result.pts == 11 and result.is_keyframe
    assert capture.video_bytes == 0


@pytest.mark.asyncio
async def test_rotation_discards_old_gop_and_requires_new_configuration(capture):
    reader = capture.video_reader = asyncio.StreamReader()
    capture.tasks.append(asyncio.create_task(capture._read_video()))
    reader.feed_data(session() + packet(SPS_PPS, config=True) + packet(IDR, keyframe=True))
    await asyncio.wait_for(capture.video_ready.wait(), 1)
    capture.video_ready.clear()
    reader.feed_data(session(720, 1280, True) + packet(DELTA, 1100000) +
        packet(IDR, 1133333, keyframe=True) + packet(SPS_PPS, config=True) + packet(IDR, 1166666, keyframe=True))
    await asyncio.wait_for(capture.video_ready.wait(), 1)
    result = await capture.video()
    assert (capture.width, capture.height) == (720, 1280)
    assert result.pts == 1166666 and result.is_keyframe


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', [struct.pack('>III', 0x80000002, 640, 360),
    struct.pack('>III', 0x80000000, 0, 360), struct.pack('>QI', 123, media.MAX_VIDEO_PACKET + 1),
    session() + packet(b'not-h264'), session() + packet(SPS_PPS.replace(b'\x67\x42', b'\x67\x64'), config=True)])
async def test_invalid_video_headers_and_profile_fail_closed(capture, bad):
    capture.video_reader = asyncio.StreamReader()
    capture.video_reader.feed_data(bad)
    await capture._read_video()
    assert isinstance(capture.video_error, HTTPException)
    with pytest.raises(HTTPException):
        await capture.video()


@pytest.mark.asyncio
async def test_raw_audio_becomes_stereo_frame_without_clock_reset(capture):
    capture.audio_available = True
    capture.audio_reader = asyncio.StreamReader()
    capture.tasks.append(asyncio.create_task(capture._read_audio()))
    pcm = b'\x01\x00\xfe\xff' * 1024
    timestamp = 987654321000
    capture.audio_reader.feed_data(packet(pcm, timestamp))
    frame = await asyncio.wait_for(capture.audio(), 1)
    assert frame.format.name == 's16' and frame.layout.name == 'stereo'
    assert frame.samples == 1024 and frame.sample_rate == 48000
    assert frame.pts == timestamp and frame.time_base == Fraction(1, 1000000)
    assert bytes(frame.planes[0])[:len(pcm)] == pcm


@pytest.mark.asyncio
async def test_audio_overload_is_bounded_and_does_not_touch_video(capture):
    capture.audio_available = True
    capture.audio_reader = asyncio.StreamReader()
    capture.tasks.append(asyncio.create_task(capture._read_audio()))
    for index in range(20):
        capture.audio_reader.feed_data(packet(b'\0' * 4096, index * 21333))
    for _ in range(10):
        if capture.audio_queue.qsize() == 8:
            await asyncio.sleep(.01)
            break
        await asyncio.sleep(.01)
    assert capture.audio_queue.qsize() == 8
    frame = await capture.audio()
    assert frame.pts >= 12 * 21333
    assert capture.video_error is None


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', [struct.pack('>QI', 1, 4097), struct.pack('>QI', 1, 3), struct.pack('>QI', media.FLAG_CONFIG, 4)])
async def test_invalid_audio_disables_output_without_microphone_fallback(capture, bad):
    capture.audio_available = True
    capture.audio_reader = asyncio.StreamReader()
    capture.audio_reader.feed_data(bad)
    await capture._read_audio()
    assert capture.audio_available is False and '不会切换到麦克风' in capture.audio_reason
    with pytest.raises(HTTPException): await capture.audio()


@pytest.mark.asyncio
async def test_close_removes_only_owned_forward_and_unblocks_consumers(capture, tmp_path, monkeypatch):
    capture.policy = SimpleNamespace(enabled=True, serials=['public-serial'])
    capture.local_spec = 'localfilesystem:' + str(tmp_path / 'capture.sock')
    capture.remote_spec = 'localabstract:scrcpy_1234abcd'
    listing = ('other localfilesystem:/tmp/unrelated localabstract:scrcpy_other\n' +
               'public-serial ' + capture.local_spec + ' ' + capture.remote_spec).encode()
    runner = AsyncMock(return_value=listing)
    monkeypatch.setattr(capture, '_adb', runner)
    pending = asyncio.create_task(capture.video())
    await asyncio.sleep(0)
    await capture.close()
    await capture.close()
    with pytest.raises(HTTPException): await pending
    assert runner.await_count == 2
    assert runner.call_args.args[0] == ['forward', '--remove', capture.local_spec]


@pytest.mark.asyncio
async def test_close_does_not_remove_replaced_forward(capture, monkeypatch):
    capture.policy = SimpleNamespace(enabled=True, serials=['public-serial'])
    capture.local_spec, capture.remote_spec = 'localfilesystem:/tmp/private.sock', 'localabstract:scrcpy_expected'
    runner = AsyncMock(return_value=b'public-serial localfilesystem:/tmp/private.sock localabstract:scrcpy_other')
    monkeypatch.setattr(capture, '_adb', runner)
    await capture.close()
    assert runner.await_count == 1


@pytest.mark.asyncio
async def test_close_keeps_cleanup_running_when_one_waiter_is_cancelled(capture, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    original = capture._close_resources
    async def delayed():
        started.set()
        await release.wait()
        await original()
    monkeypatch.setattr(capture, '_close_resources', delayed)
    first = asyncio.create_task(capture.close())
    await started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError): await first
    assert capture.closed and not capture.close_task.done()
    second = asyncio.create_task(capture.close())
    await asyncio.sleep(0)
    assert not second.done()
    release.set()
    await second
    assert capture.close_task.done()


@pytest.mark.asyncio
async def test_close_failure_remains_visible_to_later_waiters(capture, monkeypatch):
    async def fail():
        raise HTTPException(503, '公开测试清理失败')
    monkeypatch.setattr(capture, '_close_resources', fail)
    for _ in range(2):
        with pytest.raises(HTTPException) as error:
            await capture.close()
        assert error.value.status_code == 503
    # 本用例没有实际资源，恢复已完成清理任务，避免 fixture 重复抛出同一预期错误。
    capture.close_task = asyncio.create_task(asyncio.sleep(0))


@pytest.mark.asyncio
async def test_explicit_audio_fatal_rebuilds_video_once_without_profile_fallback(tmp_path, monkeypatch):
    instances = []
    async def start(value):
        instances.append(value)
        if value.audio_requested:
            await value.close()
            raise media.AudioSetupUnavailable('公开测试音频配置被拒绝')
        value.width, value.height, value.started = 1280, 720, True
        return value
    monkeypatch.setattr(media.AndroidMediaCapture, 'start', start)
    result = await media.create_capture(SimpleNamespace(state_dir=tmp_path), 'public-serial', audio_enabled=True)
    assert len(instances) == 2 and instances[0].closed
    assert result.audio_requested and not result.audio_available
    assert '公开测试音频配置被拒绝' in result.audio_reason and '仅画面连接' in result.audio_reason
    assert (result.width, result.height) == (1280, 720)
    await result.close()


@pytest.mark.asyncio
async def test_video_profile_failure_never_restarts_or_changes_profile(tmp_path, monkeypatch):
    calls = []
    async def start(value):
        calls.append(value)
        raise HTTPException(503, '设备未遵守 Baseline')
    monkeypatch.setattr(media.AndroidMediaCapture, 'start', start)
    with pytest.raises(HTTPException):
        await media.create_capture(SimpleNamespace(state_dir=tmp_path), 'public-serial', audio_enabled=True)
    assert len(calls) == 1
