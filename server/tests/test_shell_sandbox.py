import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

from homeai import shell_sandbox as sandbox

IMAGE = 'sha256:' + 'a' * 64


def valid(**changes):
    return dict(script='printf hello', inputs={}, outputs=[], timeout_seconds=5, image=IMAGE, **changes)


@pytest.mark.parametrize('path', ['/etc/passwd', '../secret', 'a/../b', './x', 'a//b', 'a/', 'a\\b', 'a\n'])
def test_rejects_paths_outside_workspace(path):
    with pytest.raises(sandbox.SandboxError):
        sandbox.validate_request('true', {}, [path], 5, IMAGE)


@pytest.mark.parametrize('image', ['python:latest', 'python:3.13', 'sha256:abc', '--privileged', IMAGE + '\n'])
def test_rejects_mutable_or_invalid_image(image):
    with pytest.raises(sandbox.SandboxError):
        sandbox.validate_request('true', {}, [], 5, image)


def test_rejects_overlapping_input_files_and_duplicate_outputs():
    with pytest.raises(sandbox.SandboxError):
        sandbox.validate_request('true', {'a': b'', 'a/b': b''}, [], 5, IMAGE)
    with pytest.raises(sandbox.SandboxError):
        sandbox.validate_request('true', {}, ['a', 'a'], 5, IMAGE)


@pytest.mark.parametrize('timeout', [0, 301, True, 1.5])
def test_rejects_invalid_timeout(timeout):
    with pytest.raises(sandbox.SandboxError):
        sandbox.validate_request('true', {}, [], timeout, IMAGE)


def test_rejects_input_size_limit(monkeypatch):
    monkeypatch.setattr(sandbox, 'MAX_INPUT_BYTES', 3)
    with pytest.raises(sandbox.SandboxError):
        sandbox.validate_request('true', {'one': b'12', 'two': b'34'}, [], 5, IMAGE)


def test_accepts_unicode_paths_and_digest_image():
    sandbox.validate_request('true', {'资料/文件.txt': b'abc'}, ['报告.csv'], 5,
                             'registry.example/python@sha256:' + 'b' * 64, 'runsc')


def test_container_restrictions_and_separate_watchdog_uid():
    args = sandbox._create_args('test', 'id', IMAGE, 'runsc', 3)
    pairs = {args[i]: args[i + 1] for i in range(len(args) - 1) if args[i].startswith('--')}
    assert pairs['--network'] == 'none'
    assert pairs['--user'] == '65531:65531'
    assert pairs['--cap-drop'] == 'ALL'
    assert pairs['--security-opt'] == 'no-new-privileges'
    assert pairs['--memory'] == '512m'
    assert pairs['--pids-limit'] == '64'
    assert pairs['--runtime'] == 'runsc'
    assert pairs['--pull'] == 'never'
    assert '--read-only' in args and '--rm' in args and '--init' in args
    assert '--privileged' not in args and '--volume' not in args and '--mount' not in args
    assert not any('docker.sock' in item for item in args)
    assert 'time.sleep(93)' in args[-1]


class DockerFake:
    def __init__(self):
        self.calls = []
        self.rows = {}
        self.result = sandbox._Result(0, b'done\n', b'')
        self.files = {'报告.txt': b'complete'}
        self.timeout = False

    async def __call__(self, args, *, data=None, **kwargs):
        self.calls.append((args, data, kwargs))
        if args[:2] == ['image', 'inspect']:
            return sandbox._Result(0, b'null', b'')
        if args[0] == 'inspect':
            row = self.rows.get(args[-1])
            return sandbox._Result(0, json.dumps([row]).encode(), b'') if row else sandbox._Result(1, b'', b'No such object')
        if args[0] == 'create':
            labels = dict(item.split('=', 1) for i, item in enumerate(args) if i and args[i - 1] == '--label')
            self.rows[args[2]] = {'Id': 'b' * 64, 'Config': {'Labels': labels}}
            return sandbox._Result(0, b'b' * 64 + b'\n', b'')
        if args[0] == 'rm':
            for name, row in list(self.rows.items()):
                if row['Id'] == args[-1]:
                    del self.rows[name]
            return sandbox._Result(0, b'', b'')
        if sandbox._PREPARE in args:
            return sandbox._Result(0, b'READY\n', b'')
        if sandbox._COLLECT in args:
            path, maximum = json.loads(data)
            raw = self.files[path]
            return sandbox._Result(0, raw[:maximum], b'', len(raw) > maximum)
        if '/bin/sh' in args:
            if self.timeout:
                raise TimeoutError()
            return self.result
        return sandbox._Result(0, b'', b'')


@pytest.mark.asyncio
async def test_executes_stdin_script_with_inputs_and_binary_outputs(monkeypatch):
    docker = DockerFake()
    monkeypatch.setattr(sandbox, '_command', docker)
    result = await sandbox.run_sandbox(script='printf done', inputs={'输入.txt': b'private'},
                                       outputs=['报告.txt'], timeout_seconds=5, image=IMAGE)
    assert result == dict(exit_code=0, stdout='done\n', stderr='', truncated=False, files={'报告.txt': b'complete'})
    assert not docker.rows
    shell = next(call for call in docker.calls if '/bin/sh' in call[0])
    assert shell[1] == b'printf done' and 'printf done' not in shell[0]
    assert shell[0][shell[0].index('--user') + 1] == '65532:65532'
    assert not any(b'private' in str(call[0]).encode() for call in docker.calls)


@pytest.mark.asyncio
async def test_nonzero_does_not_publish_partial_outputs(monkeypatch):
    docker = DockerFake()
    docker.result = sandbox._Result(42, b'partial', b'failure')
    monkeypatch.setattr(sandbox, '_command', docker)
    result = await sandbox.run_sandbox(script='exit 42', inputs={}, outputs=['报告.txt'], timeout_seconds=5, image=IMAGE)
    assert result['exit_code'] == 42 and result['files'] == {}
    assert not any(sandbox._COLLECT in call[0] for call in docker.calls)
    assert not docker.rows


@pytest.mark.asyncio
async def test_timeout_is_dispatched_and_container_is_removed(monkeypatch):
    docker = DockerFake()
    docker.timeout = True
    monkeypatch.setattr(sandbox, '_command', docker)
    with pytest.raises(sandbox.SandboxError) as error:
        await sandbox.run_sandbox(**valid())
    assert error.value.dispatched is True
    assert '超时' in str(error.value)
    assert not docker.rows


@pytest.mark.asyncio
async def test_cancellation_before_dispatch_does_not_create_container(monkeypatch):
    docker = DockerFake()
    monkeypatch.setattr(sandbox, '_command', docker)
    async def cancelled():
        return True
    with pytest.raises(sandbox.SandboxError) as error:
        await sandbox.run_sandbox(**valid(), cancelled=cancelled)
    assert not error.value.dispatched and not docker.calls


@pytest.mark.asyncio
async def test_cleanup_cannot_remove_another_run(monkeypatch):
    docker = DockerFake()
    docker.rows['same-name'] = {'Id': 'b' * 64, 'Config': {'Labels': {sandbox._LABEL + '.run': 'other'}}}
    monkeypatch.setattr(sandbox, '_command', docker)
    assert await sandbox._remove_owned('same-name', 'mine')
    assert 'same-name' in docker.rows
    assert not any(call[0][0] == 'rm' for call in docker.calls)


@pytest.mark.asyncio
async def test_all_slots_busy_does_not_remove_live_containers(monkeypatch):
    docker = DockerFake()
    for slot in range(sandbox.MAX_PARALLEL):
        docker.rows[f'homeai-shell-sandbox-{slot}'] = {
            'Id': 'b' * 64, 'Config': {'Labels': {sandbox._LABEL: 'v1', sandbox._LABEL + '.run': 'other',
                                               sandbox._LABEL + '.expires': str(time.time() + 500)}}}
    monkeypatch.setattr(sandbox, '_command', docker)
    with pytest.raises(sandbox.SandboxError) as error:
        await sandbox.run_sandbox(**valid())
    assert not error.value.dispatched
    assert len(docker.rows) == sandbox.MAX_PARALLEL


@pytest.mark.asyncio
async def test_abandoned_expired_container_is_reaped(monkeypatch):
    docker = DockerFake()
    docker.rows['homeai-shell-sandbox-0'] = {
        'Id': 'c' * 64, 'Config': {'Labels': {sandbox._LABEL: 'v1', sandbox._LABEL + '.run': 'old',
                                           sandbox._LABEL + '.expires': str(time.time() - 500)}}}
    monkeypatch.setattr(sandbox, '_command', docker)
    assert (await sandbox.run_sandbox(**valid()))['exit_code'] == 0
    assert any(call[0] == ['rm', '--force', 'c' * 64] for call in docker.calls)
    assert not docker.rows


@pytest.mark.asyncio
async def test_daemon_error_is_not_mistaken_for_missing_container(monkeypatch):
    async def unavailable(*args, **kwargs):
        return sandbox._Result(1, b'', b'Cannot connect to Docker daemon')
    monkeypatch.setattr(sandbox, '_command', unavailable)
    with pytest.raises(sandbox.SandboxError, match='Docker'):
        await sandbox._inspect('some-container')


@pytest.mark.asyncio
async def test_subprocess_bounds_both_streams_and_does_not_inherit_secrets(monkeypatch):
    monkeypatch.setattr(sandbox.shutil, 'which', lambda _: sys.executable)
    monkeypatch.setenv('HOMEAI_TEST_SECRET', 'private')
    code = "import os,sys; assert 'HOMEAI_TEST_SECRET' not in os.environ; print('a'*100); print('b'*100,file=sys.stderr)"
    result = await sandbox._command(['-I', '-c', code], limit=17)
    assert result.code == 0 and result.truncated
    assert result.stdout == b'a' * 17 and result.stderr == b'b' * 17


def collect(output, path, limit=1024):
    code = sandbox._COLLECT.replace("'/workspace/output'", repr(str(output)))
    return subprocess.run([sys.executable, '-I', '-c', code], input=json.dumps([path, limit]).encode(),
                          capture_output=True, timeout=5)


def test_reader_handles_real_unicode_binary_file(tmp_path):
    (tmp_path / '资料').mkdir()
    (tmp_path / '资料' / '结果.bin').write_bytes(b'\x00\xffreal')
    result = collect(tmp_path, '资料/结果.bin')
    assert result.returncode == 0 and result.stdout == b'\x00\xffreal'


@pytest.mark.parametrize('kind', ['symlink', 'directory_link', 'hardlink', 'fifo', 'directory', 'missing', 'oversize'])
def test_reader_rejects_unsafe_or_missing_files(tmp_path, kind):
    root = tmp_path / 'output'
    root.mkdir()
    (tmp_path / 'outside').write_bytes(b'private')
    if kind == 'symlink':
        (root / 'result').symlink_to(tmp_path / 'outside')
    elif kind == 'directory_link':
        (root / 'result').symlink_to(tmp_path, target_is_directory=True)
    elif kind == 'hardlink':
        os.link(tmp_path / 'outside', root / 'result')
    elif kind == 'fifo':
        os.mkfifo(root / 'result')
    elif kind == 'directory':
        (root / 'result').mkdir()
    elif kind == 'oversize':
        (root / 'result').write_bytes(b'x' * 2048)
    result = collect(root, 'result/outside' if kind == 'directory_link' else 'result')
    assert result.returncode != 0 and result.stdout == b''


@pytest.mark.asyncio
async def test_live_real_container_files_limits_and_cleanup():
    image = os.environ.get('HOMEAI_SHELL_SANDBOX_IMAGE')
    if not image:
        pytest.skip('需要已安装固定镜像和 Docker，设置 HOMEAI_SHELL_SANDBOX_IMAGE 运行真实隔离测试')
    script = '''python3 -I - <<'PYCODE'
import os, pathlib, socket
assert os.getuid() == 65532
assert not pathlib.Path('/var/run/docker.sock').exists()
assert not pathlib.Path('/workspace/input/../../master.key').exists()
assert 'HOMEAI_TEST_SECRET' not in os.environ
status = pathlib.Path('/proc/self/status').read_text()
assert 'NoNewPrivs:\t1' in status and 'CapEff:\t0000000000000000' in status
try:
    socket.create_connection(('1.1.1.1', 443), timeout=1)
except OSError:
    pass
else:
    raise AssertionError('隔离容器可以访问外网')
try:
    pathlib.Path('/root-write-probe').write_text('no')
except OSError:
    pass
else:
    raise AssertionError('根文件系统可写')
source = pathlib.Path('/workspace/input/原文.txt').read_bytes()
pathlib.Path('结果.txt').write_bytes(source.upper())
print('REAL_SANDBOX_OK')
PYCODE'''
    result = await sandbox.run_sandbox(script=script, inputs={'原文.txt': b'actual input'},
                                       outputs=['结果.txt'], timeout_seconds=15, image=image)
    assert result['exit_code'] == 0 and result['stdout'].strip() == 'REAL_SANDBOX_OK'
    assert result['files'] == {'结果.txt': b'ACTUAL INPUT'}
    failure = await sandbox.run_sandbox(script='exit 23', inputs={}, outputs=[], timeout_seconds=3, image=image)
    assert failure['exit_code'] == 23
    with pytest.raises(sandbox.SandboxError) as error:
        await sandbox.run_sandbox(script='sleep 30', inputs={}, outputs=[], timeout_seconds=1, image=image)
    assert error.value.dispatched
    for slot in range(sandbox.MAX_PARALLEL):
        assert await sandbox._inspect(f'homeai-shell-sandbox-{slot}') is None


@pytest.mark.asyncio
async def test_cancel_after_dispatch_removes_running_container(monkeypatch):
    docker = DockerFake()
    original = docker.__call__
    entered = asyncio.Event()
    async def command(args, **kwargs):
        if '/bin/sh' in args:
            entered.set()
            await asyncio.sleep(30)
        return await original(args, **kwargs)
    async def cancelled():
        return entered.is_set()
    monkeypatch.setattr(sandbox, '_command', command)
    with pytest.raises(sandbox.SandboxError) as error:
        await sandbox.run_sandbox(**valid(), cancelled=cancelled)
    assert error.value.dispatched and not docker.rows


@pytest.mark.asyncio
async def test_image_volumes_rejected_before_dispatch(monkeypatch):
    async def image(*args, **kwargs):
        return sandbox._Result(0, b'{"/hidden-volume": {}}', b'')
    monkeypatch.setattr(sandbox, '_command', image)
    with pytest.raises(sandbox.SandboxError, match='数据卷') as error:
        await sandbox.run_sandbox(**valid())
    assert not error.value.dispatched


@pytest.mark.asyncio
async def test_unconfirmed_cleanup_keeps_dispatched_flag(monkeypatch):
    docker = DockerFake()
    monkeypatch.setattr(sandbox, '_command', docker)
    async def cleanup(*args):
        raise sandbox.SandboxError('daemon unavailable')
    monkeypatch.setattr(sandbox, '_remove_owned', cleanup)
    with pytest.raises(sandbox.SandboxError, match='清理') as error:
        await sandbox.run_sandbox(**valid())
    assert error.value.dispatched


@pytest.mark.asyncio
async def test_invalid_inspect_response_fails_closed(monkeypatch):
    async def invalid(*args, **kwargs):
        return sandbox._Result(0, b'not json', b'')
    monkeypatch.setattr(sandbox, '_command', invalid)
    with pytest.raises(sandbox.SandboxError, match='无法核实'):
        await sandbox._inspect('test')
