"""一次性读取服务器信息；调用方必须先完成身份、范围及逐次审批检查。"""
import asyncio
import ctypes
import os
import platform
import re
import signal
import stat
import struct
import tempfile
import time
import zlib
from pathlib import Path

from fastapi import HTTPException


MAX_COMMAND_OUTPUT = 128 * 1024
MAX_SCREEN_BYTES = 20 * 1024 * 1024
_ENVIRONMENT = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'C', 'LC_ALL': 'C'}


async def _stop(process):
    # 无论命令是否恰好结束，都回收同组子进程；不触碰其他进程组。
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), 0.3)
    except TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


async def _run(argv, *, timeout=5):
    """仅供本模块固定参数调用；限制输出、执行时间并响应取消。"""
    spawning = asyncio.create_task(asyncio.create_subprocess_exec(
        *argv, env=_ENVIRONMENT, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True, limit=32768))
    try:
        process = await asyncio.shield(spawning)
    except asyncio.CancelledError:
        try:
            process = await spawning
        except OSError:
            raise asyncio.CancelledError from None
        await _stop(process)
        raise
    except OSError:
        raise HTTPException(503, '服务器缺少所需系统读取工具，或当前服务账户无权执行') from None

    async def read(stream):
        output = bytearray()
        while chunk := await stream.read(16384):
            output.extend(chunk)
            if len(output) > MAX_COMMAND_OUTPUT:
                raise HTTPException(502, '系统读取工具输出超过安全限制，本次未使用其结果')
        return bytes(output)

    readers = [asyncio.create_task(read(process.stdout)), asyncio.create_task(read(process.stderr))]
    try:
        async with asyncio.timeout(timeout):
            stdout, _ = await asyncio.gather(*readers)
            await process.wait()
        if process.returncode:
            raise HTTPException(503, '系统读取失败，请检查服务账户权限和当前系统环境')
        return stdout.decode('utf-8', 'strict')
    except TimeoutError:
        raise HTTPException(504, '服务器信息采集超时，请稍后重试') from None
    except UnicodeError:
        raise HTTPException(502, '系统读取工具返回了无法解析的内容') from None
    finally:
        await _stop(process)
        for reader in readers:
            if not reader.done():
                reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


def _mac_cpu(raw):
    matches = re.findall(r'CPU usage:\s*([\d.]+)% user,\s*([\d.]+)% sys,\s*([\d.]+)% idle', raw)
    if len(matches) < 2:
        raise HTTPException(502, 'CPU 采样不完整，未将单次累计统计当成实时使用率')
    try:
        user, system, idle = map(float, matches[-1])
    except ValueError:
        raise HTTPException(502, 'CPU 统计格式异常，未使用本次结果') from None
    if any(not 0 <= value <= 100 for value in (user, system, idle)) or not 99 <= user + system + idle <= 101:
        raise HTTPException(502, 'CPU 统计格式异常，未使用本次结果')
    return {'usage_percent': round(100 - idle, 2), 'user_percent': user, 'system_percent': system,
            'idle_percent': idle, 'sample_interval_seconds': 1.0}


def _mac_memory(total_raw, pages_raw):
    try:
        total = int(total_raw.strip())
        page_size = int(re.search(r'page size of (\d+) bytes', pages_raw).group(1))
        pages = {key.strip(): int(value) for key, value in re.findall(r'^([^:\n]+):\s*(\d+)\.?\s*$', pages_raw, re.M)}
        keys = {'free_bytes': 'Pages free', 'active_bytes': 'Pages active', 'inactive_bytes': 'Pages inactive',
                'wired_bytes': 'Pages wired down', 'compressed_bytes': 'Pages occupied by compressor'}
        values = {key: pages[name] * page_size for key, name in keys.items()}
        if total <= 0 or page_size <= 0 or any(value > total for value in values.values()):
            raise ValueError('内存统计超出物理容量')
    except (ValueError, AttributeError, KeyError):
        raise HTTPException(502, '内存页统计不完整，未使用本次结果') from None
    return {'total_bytes': total, **values, 'page_size_bytes': page_size,
            'available_bytes': None, 'compressed_basis': 'physical_compressor_pages',
            'accounting_note': '压缩值表示压缩器实际占用的物理页；空闲页不等同于系统可回收内存。'}


def _read_proc(path):
    try:
        with open(path, 'rb') as source:
            raw = source.read(MAX_COMMAND_OUTPUT + 1)
        if len(raw) > MAX_COMMAND_OUTPUT:
            raise ValueError('系统统计超限')
        return raw.decode('ascii')
    except (OSError, ValueError, UnicodeError):
        raise HTTPException(503, '当前服务器未提供可读的系统统计，请检查 /proc 挂载及服务账户权限') from None


def _linux_cpu_counters(raw):
    try:
        first = raw.splitlines()[0].split()
        if first[0] != 'cpu' or len(first) < 5:
            raise ValueError('CPU 统计缺失')
        values = [int(value) for value in first[1:9]]
        if any(value < 0 for value in values):
            raise ValueError('CPU 计数无效')
        return values + [0] * (8 - len(values))
    except (IndexError, ValueError):
        raise HTTPException(502, 'CPU 系统计数无法解析') from None


def _linux_cpu(before, after, interval):
    delta = [new - old for old, new in zip(before, after)]
    total = sum(delta)
    if any(value < 0 for value in delta) or total <= 0:
        raise HTTPException(502, 'CPU 两次采样无有效增量，请稍后重试')
    # guest 已包含在 user/nice，读取前八列避免重复计算。
    idle = delta[3] + delta[4]
    return {'usage_percent': round(100 * (total - idle) / total, 2),
            'user_percent': round(100 * (delta[0] + delta[1]) / total, 2),
            'system_percent': round(100 * (delta[2] + delta[5] + delta[6]) / total, 2),
            'idle_percent': round(100 * idle / total, 2),
            'sample_interval_seconds': round(interval, 3)}


def _linux_memory(raw):
    try:
        values = {key: int(amount) * 1024 for key, amount in re.findall(r'^(\w+):\s*(\d+) kB\s*$', raw, re.M)}
        result = {'total_bytes': values['MemTotal'], 'free_bytes': values['MemFree'],
                  'active_bytes': values['Active'], 'inactive_bytes': values['Inactive'],
                  'available_bytes': values.get('MemAvailable'), 'wired_bytes': None, 'compressed_bytes': None}
        if result['total_bytes'] <= 0 or any(value is not None and value > result['total_bytes'] for key, value in result.items() if key != 'total_bytes'):
            raise ValueError('内存统计超出物理容量')
    except (KeyError, ValueError):
        raise HTTPException(502, '内存系统统计无法解析') from None
    return {**result, 'page_size_bytes': os.sysconf('SC_PAGE_SIZE'), 'compressed_basis': None,
            'accounting_note': 'Linux 未提供与 macOS wired/compressed 相同口径的统计，相关字段留空。'}


async def collect_metrics(targets: list[str]) -> dict:
    if not isinstance(targets, list) or not targets or any(not isinstance(target, str) or target not in {'cpu', 'memory'} for target in targets) or len(set(targets)) != len(targets):
        raise HTTPException(422, '系统指标只允许一次性读取 CPU 或内存')
    system = platform.system()
    result = {'platform': system, 'scope': 'core_visible_os',
              'scope_note': '仅代表 Core 所在操作系统可见的统计；容器部署时不保证等于物理宿主机视图。'}
    if system == 'Darwin':
        if 'cpu' in targets:
            result['cpu'] = _mac_cpu(await _run(['/usr/bin/top', '-l', '2', '-s', '1', '-n', '0']))
        if 'memory' in targets:
            total = await _run(['/usr/sbin/sysctl', '-n', 'hw.memsize'])
            pages = await _run(['/usr/bin/vm_stat'])
            result['memory'] = _mac_memory(total, pages)
    elif system == 'Linux':
        if 'cpu' in targets:
            before = _linux_cpu_counters(_read_proc('/proc/stat'))
            start = time.monotonic()
            await asyncio.sleep(0.25)
            after = _linux_cpu_counters(_read_proc('/proc/stat'))
            result['cpu'] = _linux_cpu(before, after, time.monotonic() - start)
        if 'memory' in targets:
            result['memory'] = _linux_memory(_read_proc('/proc/meminfo'))
    else:
        raise HTTPException(503, '当前服务器系统尚不支持 CPU 与内存采集')
    return {**result, 'sampled_at': time.time()}


def _screen_permission():
    try:
        library = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
        check = library.CGPreflightScreenCaptureAccess
        check.argtypes = []
        check.restype = ctypes.c_bool
        allowed = bool(check())
    except (OSError, AttributeError):
        raise HTTPException(503, '无法核实 macOS 屏幕录制权限，本次未采集截屏') from None
    if not allowed:
        raise HTTPException(403, '服务器尚未获得 macOS 屏幕录制权限。请在服务器“系统设置 → 隐私与安全性 → 屏幕与系统音频录制”中允许实际运行 Core 的应用或服务，重启后重试；手机同意不能代替此系统授权。')


def _validated_png(raw):
    if not 45 <= len(raw) <= MAX_SCREEN_BYTES or raw[:8] != b'\x89PNG\r\n\x1a\n':
        raise HTTPException(502, '截屏文件不是有效 PNG，未保存本次结果')
    offset, chunks, has_data = 8, 0, False
    while offset + 12 <= len(raw) and chunks < 10000:
        size = struct.unpack('>I', raw[offset:offset + 4])[0]
        end = offset + 12 + size
        if end > len(raw):
            break
        kind = raw[offset + 4:offset + 8]
        content = raw[offset + 8:offset + 8 + size]
        checksum = struct.unpack('>I', raw[end - 4:end])[0]
        if zlib.crc32(kind + content) & 0xffffffff != checksum:
            break
        if chunks == 0:
            if kind != b'IHDR' or size != 13:
                break
            width, height = struct.unpack('>II', content[:8])
            if not width or not height or width > 32768 or height > 32768 or width * height > 100_000_000:
                break
        elif kind == b'IHDR':
            break
        has_data = has_data or (kind == b'IDAT' and size > 0)
        if kind == b'IEND':
            if size == 0 and has_data and end == len(raw):
                return raw
            break
        chunks += 1
        offset = end
    raise HTTPException(502, '截屏文件不完整或校验失败，未保存本次结果')


def _read_screen(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.getuid():
                raise ValueError('截屏文件不安全')
            if not 0 < before.st_size <= MAX_SCREEN_BYTES:
                raise ValueError('截屏超限或为空')
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'rb', closefd=False) as source:
                raw = source.read(MAX_SCREEN_BYTES + 1)
            after = os.fstat(fd)
            if len(raw) != before.st_size or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('截屏内容变化')
        finally:
            os.close(fd)
    except (OSError, ValueError):
        raise HTTPException(502, '截屏未生成可安全读取的文件，文件为空、超过 20 MiB 或发生变化') from None
    return _validated_png(raw)


async def capture_screen() -> bytes:
    if platform.system() != 'Darwin':
        raise HTTPException(503, '当前服务器尚未提供可验证的图形会话截屏工具；Linux 无图形会话时无法截屏')
    _screen_permission()
    # 明文只存在于当前账户的临时目录；成功、异常及取消都会清理。
    with tempfile.TemporaryDirectory(prefix='homeai-screen-') as directory:
        os.chmod(directory, 0o700)
        path = Path(directory) / 'capture.png'
        try:
            await _run(['/usr/sbin/screencapture', '-x', '-m', '-t', 'png', str(path)], timeout=10)
        except HTTPException as exc:
            if exc.status_code == 503:
                raise HTTPException(503, '服务器截屏工具未能完成采集，请检查图形登录会话、屏幕录制权限及实际运行 Core 的服务账户') from None
            raise
        return _read_screen(path)
