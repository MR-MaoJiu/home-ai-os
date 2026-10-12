"""在短命容器中执行 Shell；容器不取得家庭服务的文件、网络或凭据。"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

MAX_SCRIPT_BYTES = 64 * 1024
MAX_INPUT_BYTES = 50 * 1024 * 1024
MAX_OUTPUT_BYTES = 100 * 1024 * 1024
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_STREAM_BYTES = 64 * 1024
MAX_FILES = 16
MAX_TIMEOUT_SECONDS = 300
MAX_PARALLEL = 2
_LABEL = 'homeai.shell.sandbox'
_IMAGE = re.compile(r'(?:sha256:[a-f0-9]{64}|[a-zA-Z0-9][a-zA-Z0-9._/:\-]*@sha256:[a-f0-9]{64})\Z')
_RUNTIME = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}\Z')


class SandboxError(Exception):
    def __init__(self, detail: str, *, dispatched: bool = False):
        super().__init__(detail)
        self.detail = detail
        self.dispatched = dispatched


@dataclass
class _Result:
    code: int
    stdout: bytes
    stderr: bytes
    truncated: bool = False


def _relative_path(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value.encode('utf-8')) > 240
            or '\\' in value or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(part in ('', '.', '..') for part in value.split('/'))):
        raise SandboxError('文件必须使用工作区内的普通相对路径，不能包含父目录或特殊字符')
    return value


def validate_request(script: str, inputs: dict[str, bytes], outputs: list[str], timeout_seconds: int,
                     image: str, runtime: str | None = None) -> None:
    if not isinstance(script, str) or not script.strip() or '\x00' in script or len(script.encode()) > MAX_SCRIPT_BYTES:
        raise SandboxError('Shell 命令不能为空，且最多为 64 KiB')
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise SandboxError('Shell 执行时限必须为 1 至 300 秒')
    if not isinstance(image, str) or not _IMAGE.fullmatch(image):
        raise SandboxError('Shell 镜像必须是已安装的不可变 sha256 镜像 ID 或摘要')
    if runtime is not None and (not isinstance(runtime, str) or not _RUNTIME.fullmatch(runtime)):
        raise SandboxError('Shell 容器运行时名称无效')
    if not isinstance(inputs, dict) or len(inputs) > MAX_FILES:
        raise SandboxError('一次最多提供 16 个输入文件')
    if not isinstance(outputs, list) or len(outputs) > MAX_FILES:
        raise SandboxError('一次最多取回 16 个成果文件')
    total = 0
    paths = set()
    for path, raw in inputs.items():
        _relative_path(path)
        if not isinstance(raw, bytes):
            raise SandboxError('Shell 输入文件必须为已经授权读取的字节内容')
        total += len(raw)
        paths.add(path)
    if total > MAX_INPUT_BYTES:
        raise SandboxError('Shell 输入文件合计不能超过 50 MiB')
    for path in paths:
        if any('/'.join(path.split('/')[:i]) in paths for i in range(1, len(path.split('/')))):
            raise SandboxError('输入文件路径不能与另一个文件的目录重叠')
    for path in outputs:
        _relative_path(path)
    if len(set(outputs)) != len(outputs):
        raise SandboxError('成果文件路径不能重复')


def _environment() -> dict[str, str]:
    # Docker CLI 自身需要连接配置；不把 Core 的环境变量复制给容器。
    result = {'PATH': os.defpath + ':/usr/local/bin:/opt/homebrew/bin'}
    for key in ('HOME', 'DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_CERT_PATH', 'DOCKER_TLS_VERIFY', 'DOCKER_CONFIG'):
        if value := os.environ.get(key):
            result[key] = value
    return result


async def _command(argv: list[str], *, data: bytes | None = None, timeout: float = 15,
                   limit: int = MAX_STREAM_BYTES) -> _Result:
    executable = shutil.which('docker')
    if executable is None:
        raise SandboxError('服务器尚未安装 Docker，隔离 Shell 暂不可用')
    process = await asyncio.create_subprocess_exec(
        executable, *argv, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=_environment())

    async def drain(stream):
        kept, size, truncated = [], 0, False
        while chunk := await stream.read(65536):
            remaining = max(0, limit - size)
            kept.append(chunk[:remaining]) if remaining else None
            size += min(remaining, len(chunk))
            truncated = truncated or len(chunk) > remaining
        return b''.join(kept), truncated

    async def write():
        try:
            if data:
                process.stdin.write(data)
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()

    readers = [asyncio.create_task(drain(process.stdout)), asyncio.create_task(drain(process.stderr))]
    writer = asyncio.create_task(write())
    try:
        async with asyncio.timeout(timeout):
            await writer
            await process.wait()
            out, err = await asyncio.gather(*readers)
        return _Result(process.returncode, out[0], err[0], out[1] or err[1])
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        for future in [writer, *readers]:
            if not future.done():
                future.cancel()
        await asyncio.gather(writer, *readers, return_exceptions=True)


async def _inspect(name: str) -> dict | None:
    result = await _command(['inspect', '--type', 'container', name], limit=128 * 1024)
    if result.code != 0:
        error = result.stderr.decode('utf-8', 'replace').lower()
        if 'no such object' in error or 'no such container' in error:
            return None
        raise SandboxError('无法核实 Docker 容器状态，请检查 Docker 是否运行')
    try:
        body = json.loads(result.stdout)
        if not isinstance(body, list) or len(body) != 1 or not isinstance(body[0], dict):
            raise ValueError('容器状态结构无效')
        return body[0]
    except (ValueError, IndexError) as exc:
        raise SandboxError('Docker 返回的容器状态无法核实') from exc


async def _remove_owned(name: str, run_id: str) -> bool:
    row = await _inspect(name)
    if row is None:
        return True
    if row.get('Config', {}).get('Labels', {}).get(_LABEL + '.run') != run_id:
        return True
    container_id = row.get('Id', '')
    if not re.fullmatch(r'[a-f0-9]{64}', container_id):
        return False
    result = await _command(['rm', '--force', container_id])
    return result.code == 0 or await _inspect(name) is None


_PREPARE = r'''
import base64, json, os, sys
os.makedirs('/workspace/input', mode=0o700)
os.makedirs('/workspace/output', mode=0o700)
for path, encoded in json.load(sys.stdin).items():
    target = '/workspace/input/' + path
    os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    with os.fdopen(fd, 'wb') as output:
        output.write(base64.b64decode(encoded, validate=True))
print('READY')
'''

_COLLECT = r'''
import os, stat, sys, json
path, maximum = json.load(sys.stdin)
directory = os.open('/workspace/output', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    components = path.split('/')
    for part in components[:-1]:
        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        os.close(directory)
        directory = child
    fd = os.open(components[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
    with os.fdopen(fd, 'rb') as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError('成果必须是无链接的普通文件')
        if before.st_size > maximum:
            raise ValueError('成果大小超过限制')
        remaining = maximum + 1
        while chunk := source.read(min(65536, remaining)):
            remaining -= len(chunk)
            if remaining <= 0:
                raise ValueError('成果超限')
            sys.stdout.buffer.write(chunk)
        after = os.fstat(source.fileno())
        if (maximum + 1 - remaining != before.st_size or
            (before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
            (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError('成果读取期间发生变化')
finally:
    os.close(directory)
'''


def _create_args(name: str, run_id: str, image: str, runtime: str | None,
                 timeout_seconds: int) -> list[str]:
    lifetime = timeout_seconds + 90
    args = [
        'create', '--name', name, '--pull', 'never', '--rm', '--init', '--no-healthcheck',
        '--label', _LABEL + '=v1', '--label', _LABEL + '.run=' + run_id,
        '--label', _LABEL + '.expires=' + str(int(time.time()) + lifetime),
        '--network', 'none', '--ipc', 'none', '--read-only', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges', '--user', '65531:65531',
        '--memory', '512m', '--memory-swap', '512m', '--cpus', '1', '--pids-limit', '64',
        '--ulimit', 'nofile=64:64', '--ulimit', 'fsize=52428800:52428800',
        '--tmpfs', '/workspace:rw,noexec,nosuid,nodev,size=201326592,mode=1777',
        '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=16777216,mode=1777',
        '--log-driver', 'none', '--env', 'PATH=/usr/local/bin:/usr/bin:/bin',
        '--env', 'HOME=/tmp', '--env', 'LANG=C.UTF-8', '--entrypoint', 'python3',
    ]
    if runtime:
        args += ['--runtime', runtime]
    # 看门进程与任务使用不同的非 root UID；任务无法停止其独立的超时退出。
    return args + [image, '-I', '-c', f'import time; time.sleep({lifetime})']


async def _allocate(image: str, runtime: str | None, timeout_seconds: int, run_id: str) -> tuple[str, str]:
    for slot in range(MAX_PARALLEL):
        name = f'homeai-shell-sandbox-{slot}'
        row = await _inspect(name)
        if row:
            labels = row.get('Config', {}).get('Labels', {})
            try:
                expired = float(labels.get(_LABEL + '.expires', 'inf')) < time.time()
            except (ValueError, TypeError):
                expired = False
            if labels.get(_LABEL) == 'v1' and expired and labels.get(_LABEL + '.run'):
                await _remove_owned(name, labels[_LABEL + '.run'])
            else:
                continue
        try:
            created = await _command(_create_args(name, run_id, image, runtime, timeout_seconds))
            if created.code == 0:
                container_id = created.stdout.decode().strip()
                if not re.fullmatch(r'[a-f0-9]{64}', container_id):
                    await _remove_owned(name, run_id)
                    raise SandboxError('Docker 未返回有效容器标识')
                return name, container_id
            # 同名容器是跨 Core 进程的并发锁，其他运行的实例不能被清除。
            if await _inspect(name) is None:
                raise SandboxError('无法创建隔离容器，请检查已安装镜像、运行时和 Docker 资源')
        except BaseException:
            await asyncio.shield(_remove_owned(name, run_id))
            raise
    raise SandboxError('服务器的隔离 Shell 正在处理其他任务，请稍后重试')


async def run_sandbox(*, script: str, inputs: dict[str, bytes], outputs: list[str],
                      timeout_seconds: int, image: str, runtime: str | None = None,
                      cancelled: Callable[[], Awaitable[bool]] | None = None) -> dict:
    validate_request(script, inputs, outputs, timeout_seconds, image, runtime)
    if cancelled and await cancelled():
        raise SandboxError('Shell 任务已取消')
    run_id, name, dispatched = uuid.uuid4().hex, None, False
    try:
        checked = await _command(['image', 'inspect', image, '--format', '{{json .Config.Volumes}}'])
        if checked.code != 0:
            raise SandboxError('本机尚未安装指定的固定 Shell 镜像；执行器不会自动拉取镜像')
        try:
            volumes = json.loads(checked.stdout)
        except ValueError as exc:
            raise SandboxError('无法核实 Shell 镜像的卷配置') from exc
        if volumes not in (None, {}):
            raise SandboxError('Shell 镜像不能声明额外数据卷，请使用不含 VOLUME 的固定镜像')
        name, container_id = await _allocate(image, runtime, timeout_seconds, run_id)
        started = await _command(['start', container_id])
        if started.code != 0:
            raise SandboxError('隔离容器启动失败，请检查镜像是否包含 Python 3 和 /bin/sh')
        prefix = ['exec', '--user', '65532:65532', '-i', container_id]
        prepared = await _command(prefix + ['python3', '-I', '-c', _PREPARE], data=json.dumps(
            {path: base64.b64encode(raw).decode('ascii') for path, raw in inputs.items()}).encode())
        if prepared.code or prepared.stdout.strip() != b'READY':
            raise SandboxError('无法初始化隔离工作区，请检查固定镜像的 Python 3 支持')
        if cancelled and await cancelled():
            raise SandboxError('Shell 任务已取消')
        dispatched = True
        running = asyncio.create_task(_command(
            ['exec', '--user', '65532:65532', '--workdir', '/workspace/output', '-i',
             container_id, '/bin/sh', '-s'], data=script.encode(), timeout=timeout_seconds))
        try:
            while not running.done():
                await asyncio.wait({running}, timeout=0.2)
                if cancelled and await cancelled():
                    raise SandboxError('Shell 任务已取消', dispatched=True)
            result = await running
        finally:
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        files = {}
        # 失败命令保留退出码和输出，不把可能残缺的成果当成成功文件发布。
        if result.code == 0 and outputs:
            for path in outputs:
                if cancelled and await cancelled():
                    raise SandboxError('Shell 任务已取消', dispatched=True)
                maximum = min(MAX_FILE_BYTES, MAX_OUTPUT_BYTES - sum(map(len, files.values())))
                collected = await _command(prefix + ['python3', '-I', '-c', _COLLECT],
                                           data=json.dumps([path, maximum]).encode(), limit=maximum)
                if collected.code or collected.truncated:
                    raise SandboxError('无法安全读取成果：文件缺失、包含链接或特殊文件、大小超限，或仍在变化', dispatched=True)
                files[path] = collected.stdout
        return {'exit_code': result.code, 'stdout': result.stdout.decode('utf-8', 'replace'),
                'stderr': result.stderr.decode('utf-8', 'replace'), 'truncated': result.truncated,
                'files': files}
    except TimeoutError as exc:
        raise SandboxError('Shell 执行或容器操作超时，已终止隔离容器', dispatched=dispatched) from exc
    except SandboxError as exc:
        exc.dispatched = exc.dispatched or dispatched
        raise
    except asyncio.CancelledError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise SandboxError('隔离 Shell 执行失败，请检查 Docker 及运行时状态', dispatched=dispatched) from exc
    finally:
        if name is not None:
            try:
                removed = await asyncio.shield(_remove_owned(name, run_id))
                if not removed:
                    raise SandboxError('隔离容器清理未确认，等待独立超时退出后再核对任务', dispatched=dispatched)
            except (OSError, TimeoutError, SandboxError) as exc:
                raise SandboxError('无法确认隔离容器已清理，等待独立超时退出后再核对任务', dispatched=dispatched) from exc


async def sandbox_ready(image: str, runtime: str | None = None) -> dict:
    """使用真实容器核实固定镜像可执行；不触碰家庭业务数据。"""
    try:
        result = await run_sandbox(script="python3 -I -c 'print(7 * 6)'", inputs={}, outputs=[],
                                   timeout_seconds=5, image=image, runtime=runtime)
        if result['exit_code'] != 0 or result['stdout'].strip() != '42':
            return {'ready': False, 'detail': '固定镜像未通过 Python 3 和 /bin/sh 执行检查'}
        return {'ready': True, 'detail': '隔离 Shell 执行检查通过'}
    except (SandboxError, OSError, TimeoutError) as exc:
        return {'ready': False, 'detail': exc.detail if isinstance(exc, SandboxError) else 'Docker 执行检查失败'}
