"""固定触控桥仅由已授权 ADB 启动；无公开端口、无任意命令或文本接口。"""
import asyncio
import hashlib
import json
import os
import stat
from pathlib import Path
from fastapi import HTTPException
from .android_adb import load_policy, command_prefix, _execute_argv

_CANCEL_TIMEOUT = 0.3
_EXIT_TIMEOUT = 0.3
_KILL_TIMEOUT = 0.5


class TouchBridge:
    def __init__(self, process):
        self.process = process
        self.lock = asyncio.Lock()
        self.identifier = 0
        self.closed = False
        self.close_task = None
        self.errors = asyncio.create_task(self._discard_errors())

    async def _discard_errors(self):
        # 系统日志不能进入模型或普通服务日志；只持续排空管道。
        while await self.process.stderr.read(4096):
            pass

    async def _reply(self):
        try:
            raw = await asyncio.wait_for(self.process.stdout.readline(), 3)
            if not raw or len(raw) > 8192:
                raise ValueError()
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get('ok') is not True:
                raise ValueError()
            if any(type(value.get(key)) is not int or not 1 <= value[key] <= 8192 for key in ('width','height')):
                raise ValueError()
            if value.get('rotation') not in (0,1,2,3):
                raise ValueError()
            return value
        except (ValueError, asyncio.TimeoutError):
            raise HTTPException(503, '安卓系统未确认触控事件；手势已停止，请核对设备') from None

    async def _request(self, message):
        self.identifier += 1
        packet = {**message, 'id': self.identifier}
        raw = json.dumps(packet, separators=(',', ':'), allow_nan=False).encode() + b'\n'
        if len(raw) > 8192:
            raise HTTPException(422, '触点事件超过限制')
        self.process.stdin.write(raw)
        await self.process.stdin.drain()
        value = await self._reply()
        if value.get('id') != self.identifier:
            raise HTTPException(503, '触控回执顺序异常，已停止手势')
        return value

    async def send(self, message):
        async with self.lock:
            if self.closed or self.process.returncode is not None:
                raise HTTPException(409, '触控桥已关闭')
            return await self._request(message)

    async def close(self):
        # 同一个清理任务由所有关闭调用等待；调用者超时不能截断进程回收。
        if self.close_task is None:
            self.closed = True
            self.close_task = asyncio.create_task(self._close_resources())
        interrupted = None
        while True:
            try:
                await asyncio.shield(self.close_task)
                break
            except asyncio.CancelledError as error:
                if self.close_task.cancelled():
                    raise
                interrupted = error
            except BaseException:
                if interrupted is not None:
                    raise interrupted from None
                raise
        if interrupted is not None:
            raise interrupted

    async def _close_resources(self):
        try:
            try:
                async with asyncio.timeout(_CANCEL_TIMEOUT):
                    async with self.lock:
                        if self.process.returncode is None:
                            await self._request({'phase':'cancel', 'points':[]})
            except Exception:
                # 回执缺失或管道已断时继续终止执行器，不能把未知结果当作取消成功。
                pass
        finally:
            if self.process.stdin:
                try:
                    self.process.stdin.close()
                except (BrokenPipeError, ConnectionError):
                    pass
            try:
                try:
                    await asyncio.wait_for(self.process.wait(), _EXIT_TIMEOUT)
                except asyncio.TimeoutError:
                    if self.process.returncode is None:
                        try:
                            self.process.kill()
                        except ProcessLookupError:
                            pass
                    try:
                        await asyncio.wait_for(self.process.wait(), _KILL_TIMEOUT)
                    except asyncio.TimeoutError:
                        raise HTTPException(503, '触控执行器未及时退出，请核对设备后重连') from None
            finally:
                # Process 没有公开的输出管道关闭接口；进程未及时退出也须释放本机管道。
                transport = getattr(self.process, '_transport', None)
                try:
                    if transport is not None:
                        transport.close()
                finally:
                    self.errors.cancel()
                    await asyncio.gather(self.errors, return_exceptions=True)


async def create_bridge(settings, serial):
    policy = load_policy(settings)
    prefix = await command_prefix(policy, serial)
    path = Path(settings.state_dir) / 'android-input' / 'homeai-touch.jar'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ValueError()
            raw = source.read(10 * 1024 * 1024 + 1)
        if not 0 < len(raw) <= 10 * 1024 * 1024:
            raise ValueError()
    except (OSError, ValueError):
        raise HTTPException(409, '尚未构建本机触控组件，请运行 android_input.py build') from None
    digest = hashlib.sha256(raw).hexdigest()
    remote = '/data/local/tmp/homeai-touch-' + digest + '.jar'
    # 文件名来自本机内容摘要，设备或模型不能指定 classpath。
    await _execute_argv(prefix + ['push', str(path), remote], timeout=15)
    checksum = await _execute_argv(prefix + ['shell','sha256sum',remote])
    if checksum.decode().split()[0] != digest:
        raise HTTPException(409, '触控组件校验失败')
    environment = {key: os.environ[key] for key in ('HOME','USER','TMPDIR') if key in os.environ}
    environment['PATH'] = '/usr/bin:/bin'
    process = await asyncio.create_subprocess_exec(*prefix, 'shell',
        'CLASSPATH=' + remote, 'app_process', '/', 'dev.homeai.androidinput.TouchServer',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=environment, limit=16384)
    bridge = TouchBridge(process)
    try:
        ready = await bridge._reply()
        if ready.get('ready') is not True:
            raise HTTPException(503, '安卓触控组件尚未就绪')
        return bridge
    except BaseException:
        try:
            await bridge.close()
        except Exception:
            pass
        raise
