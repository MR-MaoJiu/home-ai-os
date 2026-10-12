"""执行本机预登记的完整宿主脚本；不是模型可传任意命令的通道。"""
import asyncio
import fcntl
import inspect
import os
import pwd
import signal
import stat
from pathlib import Path

from fastapi import HTTPException

from .shell_configuration import HostAction, fingerprint


OUTPUT_BYTES = 128 * 1024


class HostExecutionError(HTTPException):
    def __init__(self, detail, dispatched=False):
        super().__init__(409 if dispatched else 422, detail)
        self.dispatched = dispatched


def _lock_root():
    # 使用固定的系统临时目录，避免不同 Core 实例因各自 state_dir/TMPDIR 而绕过串行执行。
    return Path('/tmp') / f'homeai-host-{os.getuid()}'


def _acquire_lock():
    directory = descriptor = None
    try:
        root = _lock_root()
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            pass
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('锁目录权限无效')
        descriptor = os.open('execution.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                             0o600, dir_fd=directory)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ValueError('锁文件必须是当前账户独占的普通文件')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HostExecutionError('另一个宿主管理动作仍在执行，请结束或核对该动作后重试') from None
        current = os.stat('execution.lock', dir_fd=directory, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError('锁文件在取得期间发生变化')
        acquired, descriptor = descriptor, None
        return acquired
    except HostExecutionError:
        raise
    except (OSError, ValueError):
        raise HostExecutionError('无法取得安全的宿主管理执行锁，请检查服务器本机锁目录权限') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def _environment():
    """脚本使用服务账户自身权限；不继承服务进程中的数据库、云密钥或代理。"""
    return {'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
            'HOME': pwd.getpwuid(os.getuid()).pw_dir, 'LANG': 'C', 'LC_ALL': 'C', 'SHELL': '/bin/sh'}


def _signal_group(process, value):
    try:
        os.killpg(process.pid, value)
    except ProcessLookupError:
        pass


async def _stop_group(process):
    _signal_group(process, signal.SIGTERM)
    await asyncio.sleep(0.15)
    _signal_group(process, signal.SIGKILL)
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except asyncio.TimeoutError:
        raise HostExecutionError('宿主进程未能确认停止，请在服务器核对执行状态', dispatched=True) from None


async def _discard(stream):
    while await stream.read(8192):
        pass


async def run_host(action, cancelled=None):
    """调用者须先重新读取本机策略并核对审批指纹；此处再次校验实际执行参数。"""
    try:
        registered = HostAction.model_validate({key: action[key] for key in HostAction.model_fields})
        if fingerprint(registered) != action['action_fingerprint'] or action.get('fingerprint') != action['action_fingerprint']:
            raise ValueError('动作指纹不匹配')
        if not isinstance(action.get('policy_fingerprint'), str) or len(action['policy_fingerprint']) != 64:
            raise ValueError('策略指纹缺失')
        if not os.path.isdir(registered.cwd):
            raise ValueError('目录不存在')
    except (KeyError, ValueError, TypeError):
        raise HostExecutionError('宿主管理动作与已审批配置不一致，请重新审批') from None
    if cancelled:
        value = cancelled()
        if inspect.isawaitable(value):
            value = await value
        if value:
            raise HostExecutionError('任务已取消，宿主命令未执行')
    lock = _acquire_lock()
    try:
        return await _execute_host(registered, cancelled, lock)
    finally:
        # 仅关闭本进程的描述符。Core 崩溃时，子进程继承的同一锁仍阻止其他维护动作启动。
        os.close(lock)


async def _execute_host(registered, cancelled, lock):
    spawning = asyncio.create_task(asyncio.create_subprocess_exec('/bin/sh', '-c', registered.script, cwd=registered.cwd,
            env=_environment(), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True, pass_fds=(lock,), limit=65536))
    try:
        # 即使恰好在派生进程时取消任务，也先拿到进程句柄再回收，不能留下无主进程。
        process = await asyncio.shield(spawning)
    except asyncio.CancelledError:
        try:
            process = await spawning
        except OSError:
            raise HostExecutionError('任务已取消，宿主命令未启动') from None
        drains = [asyncio.create_task(_discard(process.stdout)), asyncio.create_task(_discard(process.stderr))]
        try:
            await _stop_group(process)
        finally:
            for task in drains:
                task.cancel()
            await asyncio.gather(*drains, return_exceptions=True)
        raise HostExecutionError('宿主命令已取消；已发生的操作需要核对，不能自动重试', dispatched=True) from None
    except OSError:
        raise HostExecutionError('宿主命令无法启动，请检查服务账户权限和固定工作目录') from None
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    truncated = False

    async def read_output(name, stream):
        nonlocal truncated
        while chunk := await stream.read(8192):
            remaining = max(0, OUTPUT_BYTES - sum(len(buffer) for buffer in buffers.values()))
            buffers[name].extend(chunk[:remaining])
            truncated = truncated or len(chunk) > remaining

    readers = [asyncio.create_task(read_output('stdout', process.stdout)), asyncio.create_task(read_output('stderr', process.stderr))]
    started = asyncio.get_running_loop().time()
    try:
        while process.returncode is None:
            if asyncio.get_running_loop().time() - started >= registered.timeout_seconds:
                raise HostExecutionError('宿主命令执行超时；已发生的操作需要核对，不能自动重试', dispatched=True)
            if cancelled:
                value = cancelled()
                if inspect.isawaitable(value):
                    value = await value
                if value:
                    raise HostExecutionError('宿主命令已取消；已发生的操作需要核对，不能自动重试', dispatched=True)
            await asyncio.sleep(0.05)
        # 直接后台派生的进程不允许脱离任务生命周期；长期服务应交由系统服务管理器。
        await _stop_group(process)
        await asyncio.wait_for(asyncio.gather(*readers), timeout=2)
    except BaseException as exc:
        try:
            await asyncio.shield(_stop_group(process))
        finally:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
        if isinstance(exc, HostExecutionError):
            raise
        if isinstance(exc, asyncio.CancelledError):
            raise HostExecutionError('宿主命令已取消；已发生的操作需要核对，不能自动重试', dispatched=True) from None
        raise HostExecutionError('宿主命令结果无法确认，请核对已发生的操作', dispatched=True) from None
    return {'exit_code': process.returncode, 'stdout': buffers['stdout'].decode('utf-8', errors='replace'),
            'stderr': buffers['stderr'].decode('utf-8', errors='replace'), 'truncated': truncated, 'files': {}}
