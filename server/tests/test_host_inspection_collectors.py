"""系统读取和截屏采集器验证；截屏只使用生成的公开测试像素，不采集真实桌面。"""
import asyncio
import os
import shlex
import struct
import sys
import zlib
from pathlib import Path

import pytest
from fastapi import HTTPException

from homeai import host_inspection as collector


def png():
    def chunk(kind, content):
        return struct.pack('>I', len(content)) + kind + content + struct.pack('>I', zlib.crc32(kind + content) & 0xffffffff)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00')) + chunk(b'IEND', b'')


def test_mac_cpu_uses_last_interval_not_lifetime_sample_or_processes():
    raw = 'CPU usage: 40.00% user, 50.00% sys, 10.00% idle\nprivate process text\nCPU usage: 5.00% user, 15.00% sys, 80.00% idle\n'
    value = collector._mac_cpu(raw)
    assert value['usage_percent'] == 20
    assert value['user_percent'] == 5 and value['system_percent'] == 15
    assert 'private' not in str(value)


@pytest.mark.parametrize('raw', ['', 'CPU usage: 1% user, 2% sys, 97% idle',
    'CPU usage: 1% user, 2% sys, 97% idle\nCPU usage: 1..2% user, 2% sys, 97% idle',
    'CPU usage: 1% user, 2% sys, 97% idle\nCPU usage: 101% user, 2% sys, 97% idle'])
def test_invalid_or_incomplete_cpu_sample_is_not_reported_as_zero(raw):
    with pytest.raises(HTTPException):
        collector._mac_cpu(raw)


def test_mac_memory_uses_physical_compressed_pages_and_keeps_available_unknown():
    raw = '''Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free: 2.
Pages active: 4.
Pages inactive: 5.
Pages wired down: 1.
Pages stored in compressor: 100.
Pages occupied by compressor: 3.
'''
    result = collector._mac_memory(str(16384 * 20), raw)
    assert result['total_bytes'] == 16384 * 20
    assert result['compressed_bytes'] == 3 * 16384
    assert result['free_bytes'] == 2 * 16384
    assert result['active_bytes'] == 4 * 16384
    assert result['inactive_bytes'] == 5 * 16384
    assert result['wired_bytes'] == 16384
    assert result['available_bytes'] is None
    with pytest.raises(HTTPException):
        collector._mac_memory('100', raw)


def test_linux_cpu_excludes_duplicated_guest_counters_and_validates_increment():
    before = collector._linux_cpu_counters('cpu 100 10 20 1000 5 5 10 0 90 5\ncpu0 private')
    after = collector._linux_cpu_counters('cpu 120 10 30 1060 5 10 15 0 100 5')
    value = collector._linux_cpu(before, after, 0.25)
    assert value['usage_percent'] == 40
    assert value['user_percent'] == 20
    assert value['system_percent'] == 20
    assert value['idle_percent'] == 60
    with pytest.raises(HTTPException):
        collector._linux_cpu(after, before, 0.25)
    with pytest.raises(HTTPException):
        collector._linux_cpu(before, before, 0.25)


def test_linux_memory_does_not_invent_wired_or_compressed_information():
    result = collector._linux_memory('MemTotal: 1000 kB\nMemFree: 100 kB\nMemAvailable: 200 kB\nActive: 300 kB\nInactive: 400 kB\n')
    assert result['total_bytes'] == 1024000 and result['available_bytes'] == 204800
    assert result['wired_bytes'] is None and result['compressed_bytes'] is None
    with pytest.raises(HTTPException):
        collector._linux_memory('MemTotal: 100 kB\nMemFree: 200 kB')


async def test_metrics_use_fixed_mac_commands_and_only_requested_targets(monkeypatch):
    monkeypatch.setattr(collector.platform, 'system', lambda: 'Darwin')
    commands = []
    async def run(argv, **kwargs):
        commands.append(argv)
        return 'CPU usage: 1% user, 2% sys, 97% idle\nCPU usage: 3% user, 4% sys, 93% idle'
    monkeypatch.setattr(collector, '_run', run)
    value = await collector.collect_metrics(['cpu'])
    assert commands == [['/usr/bin/top', '-l', '2', '-s', '1', '-n', '0']]
    assert value['cpu']['usage_percent'] == 7 and 'memory' not in value
    assert value['scope'] == 'core_visible_os' and value['sampled_at'] > 0


@pytest.mark.parametrize('targets', [[], ['cpu', 'cpu'], ['screenshot'], ['cpu', 'shell'], ['/bin/sh']])
async def test_metrics_reject_unregistered_target_before_using_os(monkeypatch, targets):
    async def unexpected(*args, **kwargs):
        pytest.fail('未授权的目标不应运行系统工具')
    monkeypatch.setattr(collector, '_run', unexpected)
    with pytest.raises(HTTPException) as error:
        await collector.collect_metrics(targets)
    assert error.value.status_code == 422


async def test_capture_requires_system_permission_before_creating_command(monkeypatch):
    monkeypatch.setattr(collector.platform, 'system', lambda: 'Darwin')
    def denied():
        raise HTTPException(403, '服务器缺少系统屏幕录制权限')
    monkeypatch.setattr(collector, '_screen_permission', denied)
    async def unexpected(*args, **kwargs):
        pytest.fail('缺少系统权限不能执行截屏')
    monkeypatch.setattr(collector, '_run', unexpected)
    with pytest.raises(HTTPException) as error:
        await collector.capture_screen()
    assert error.value.status_code == 403


@pytest.mark.parametrize('failure', [None, 'command', 'invalid', 'cancel'])
async def test_capture_has_fixed_command_and_cleans_plaintext_in_every_outcome(monkeypatch, failure):
    monkeypatch.setattr(collector.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(collector, '_screen_permission', lambda: None)
    paths = []
    async def fake_run(argv, **kwargs):
        assert argv[:5] == ['/usr/sbin/screencapture', '-x', '-m', '-t', 'png']
        assert kwargs == {'timeout': 10}
        target = Path(argv[5]); paths.append(target)
        assert target.parent.stat().st_mode & 0o777 == 0o700
        target.write_bytes(b'not a screenshot' if failure == 'invalid' else png())
        if failure == 'command':
            raise HTTPException(503, '固定工具未完成')
        if failure == 'cancel':
            raise asyncio.CancelledError
        return ''
    monkeypatch.setattr(collector, '_run', fake_run)
    if failure:
        with pytest.raises(asyncio.CancelledError if failure == 'cancel' else HTTPException):
            await collector.capture_screen()
    else:
        assert await collector.capture_screen() == png()
    assert paths and all(not path.exists() and not path.parent.exists() for path in paths)


def test_screenshot_rejects_symbolic_hard_links_oversize_and_corrupt_png(tmp_path):
    original = tmp_path / 'original.png'; original.write_bytes(png())
    link = tmp_path / 'linked.png'; link.symlink_to(original)
    with pytest.raises(HTTPException):
        collector._read_screen(link)
    hardlink = tmp_path / 'hard.png'; os.link(original, hardlink)
    with pytest.raises(HTTPException):
        collector._read_screen(hardlink)
    oversized = tmp_path / 'large.png'
    with oversized.open('wb') as target:
        target.truncate(collector.MAX_SCREEN_BYTES + 1)
    with pytest.raises(HTTPException):
        collector._read_screen(oversized)
    with pytest.raises(HTTPException):
        collector._validated_png(png()[:-1])
    with pytest.raises(HTTPException):
        collector._validated_png(png() + b'extra')


async def test_linux_screen_is_explicitly_unavailable(monkeypatch):
    monkeypatch.setattr(collector.platform, 'system', lambda: 'Linux')
    with pytest.raises(HTTPException, match='图形会话'):
        await collector.capture_screen()


def test_screen_permission_only_preflights_and_never_requests_os_permission(monkeypatch):
    class Probe:
        def __call__(self):
            return False
    class Library:
        CGPreflightScreenCaptureAccess = Probe()
    monkeypatch.setattr(collector.ctypes, 'CDLL', lambda _: Library())
    with pytest.raises(HTTPException, match='手机同意不能代替') as error:
        collector._screen_permission()
    assert error.value.status_code == 403


async def test_fixed_runner_does_not_inherit_credentials_or_process_output(monkeypatch):
    monkeypatch.setenv('HOMEAI_DATABASE_URL', 'private-credential')
    output = await collector._run(['/usr/bin/env'])
    assert 'private-credential' not in output and 'HOMEAI_DATABASE_URL' not in output
    assert 'LC_ALL=C' in output


async def test_runner_bounds_output_and_hides_tool_stderr():
    with pytest.raises(HTTPException, match='输出超过'):
        await collector._run([sys.executable, '-I', '-c', 'print("x" * 200000)'])
    with pytest.raises(HTTPException) as error:
        await collector._run([sys.executable, '-I', '-c', 'import sys; print("private-details", file=sys.stderr); sys.exit(1)'])
    assert 'private-details' not in error.value.detail


async def test_timeout_and_cancellation_stop_spawned_process_group(tmp_path):
    for cancel in [False, True]:
        target = tmp_path / ('cancelled' if cancel else 'timedout')
        code = '(sleep 1; printf unexpected > ' + shlex.quote(str(target)) + ') & wait'
        task = asyncio.create_task(collector._run(['/bin/sh', '-c', code], timeout=10 if cancel else 0.1))
        if cancel:
            await asyncio.sleep(0.1)
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else HTTPException):
            await task
        await asyncio.sleep(1.1)
        assert not target.exists()
