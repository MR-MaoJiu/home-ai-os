import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import shlex
from types import SimpleNamespace

import pytest

from homeai.security import Actor
from homeai import shell_host
from homeai.shell_configuration import HostAction, ShellPolicy, fingerprint, host_action
from homeai.shell_host import OUTPUT_BYTES, HostExecutionError, run_host


@pytest.fixture(autouse=True)
def private_host_lock(tmp_path, monkeypatch):
    root = tmp_path / 'host-maintenance-lock'
    monkeypatch.setattr(shell_host, '_lock_root', lambda: root)
    return root


def registered(tmp_path, script, timeout=5):
    action = HostAction(id='test-action', title='隔离测试动作', script=script, cwd=str(tmp_path), timeout_seconds=timeout)
    digest = fingerprint(action)
    return {**action.model_dump(mode='json'), 'action_fingerprint': digest, 'fingerprint': digest,
            'policy_fingerprint': fingerprint(ShellPolicy(host_enabled=True, host_actions=[action]))}


def test_real_fixed_command_uses_working_directory_and_returns_exit_status(tmp_path):
    result = asyncio.run(run_host(registered(tmp_path, 'printf success; printf warning >&2; pwd; exit 7')))
    assert result['exit_code'] == 7
    assert result['stdout'] == 'success' + str(tmp_path) + '\n'
    assert result['stderr'] == 'warning'
    assert not result['truncated'] and result['files'] == {}


def test_process_does_not_inherit_server_secrets_or_proxy_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('HOMEAI_DATABASE_URL', 'sensitive-database-value')
    monkeypatch.setenv('OPENAI_API_KEY', 'sensitive-cloud-value')
    monkeypatch.setenv('HTTPS_PROXY', 'sensitive-proxy-value')
    result = asyncio.run(run_host(registered(tmp_path, '/usr/bin/env')))
    assert result['exit_code'] == 0
    assert 'sensitive-' not in result['stdout']
    assert 'HOMEAI_DATABASE_URL' not in result['stdout']
    assert 'PATH=' in result['stdout']


def test_command_receives_closed_stdin(tmp_path):
    result = asyncio.run(run_host(registered(tmp_path, 'read ignored; printf "%s" "$?"')))
    assert result['stdout'] == '1'


@pytest.mark.parametrize('field', ['script', 'cwd', 'timeout_seconds', 'id'])
def test_modified_action_is_rejected_before_dispatch(tmp_path, field):
    item = registered(tmp_path, 'printf original')
    item[field] = {'script': 'printf altered', 'cwd': '/', 'timeout_seconds': 30, 'id': 'different'}[field]
    with pytest.raises(HostExecutionError) as error:
        asyncio.run(run_host(item))
    assert not error.value.dispatched


def test_output_is_bounded_while_process_continues_to_completion(tmp_path):
    result = asyncio.run(run_host(registered(tmp_path, 'head -c 262144 /dev/zero; printf completed >&2')))
    assert result['exit_code'] == 0
    assert result['truncated']
    assert len(result['stdout'].encode()) + len(result['stderr'].encode()) == OUTPUT_BYTES


def test_non_utf8_output_is_decoded_without_losing_execution_result(tmp_path):
    result = asyncio.run(run_host(registered(tmp_path, "printf '\\377'")))
    assert result['exit_code'] == 0
    assert result['stdout'] == '\ufffd'


def test_initial_cancellation_never_dispatches(tmp_path):
    marker = tmp_path / 'never-written'
    with pytest.raises(HostExecutionError) as error:
        asyncio.run(run_host(registered(tmp_path, 'touch ' + shlex.quote(str(marker))), cancelled=lambda: True))
    assert not error.value.dispatched
    assert not marker.exists()


def test_timeout_kills_entire_process_group_including_term_ignoring_child(tmp_path):
    marker = tmp_path / 'late-side-effect'
    script = '(trap "" TERM; sleep 2; printf late > ' + shlex.quote(str(marker)) + ') & wait'

    async def execute():
        with pytest.raises(HostExecutionError) as error:
            await run_host(registered(tmp_path, script, timeout=1))
        assert error.value.dispatched
        assert '超时' in error.value.detail
        await asyncio.sleep(1.1)
        assert not marker.exists()

    asyncio.run(execute())


def test_async_cancellation_callback_stops_running_command(tmp_path):
    marker = tmp_path / 'late-side-effect'
    script = 'sleep 1; printf late > ' + shlex.quote(str(marker))

    async def execute():
        calls = 0

        async def cancelled():
            nonlocal calls
            calls += 1
            return calls > 2

        with pytest.raises(HostExecutionError) as error:
            await run_host(registered(tmp_path, script), cancelled=cancelled)
        assert error.value.dispatched
        await asyncio.sleep(1)
        assert not marker.exists()

    asyncio.run(execute())


def test_task_cancellation_stops_running_command(tmp_path):
    marker = tmp_path / 'late-side-effect'

    async def execute():
        task = asyncio.create_task(run_host(registered(tmp_path, 'sleep 1; touch ' + shlex.quote(str(marker)))))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(HostExecutionError) as error:
            await task
        assert error.value.dispatched
        await asyncio.sleep(1)
        assert not marker.exists()

    asyncio.run(execute())


def test_spawn_failure_is_not_marked_as_dispatched(tmp_path, monkeypatch):
    async def failed_spawn(*args, **kwargs):
        raise PermissionError('test')

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', failed_spawn)
    with pytest.raises(HostExecutionError) as error:
        asyncio.run(run_host(registered(tmp_path, 'printf ignored')))
    assert not error.value.dispatched


def test_cancellation_during_spawn_still_reaps_process(tmp_path, monkeypatch):
    original_spawn = asyncio.create_subprocess_exec
    marker = tmp_path / 'late-side-effect'

    async def delayed_spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        await asyncio.sleep(0.2)
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', delayed_spawn)

    async def execute():
        task = asyncio.create_task(run_host(registered(tmp_path, 'sleep 1; touch ' + shlex.quote(str(marker)))))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(HostExecutionError) as error:
            await task
        assert error.value.dispatched
        await asyncio.sleep(1)
        assert not marker.exists()

    asyncio.run(execute())


def test_finished_command_does_not_leave_background_child(tmp_path):
    marker = tmp_path / 'late-side-effect'

    async def execute():
        result = await run_host(registered(tmp_path, '(sleep 1; touch ' + shlex.quote(str(marker)) + ') & printf done'))
        assert result['exit_code'] == 0 and result['stdout'] == 'done'
        await asyncio.sleep(1)
        assert not marker.exists()

    asyncio.run(execute())


def test_real_policy_registration_runs_only_registered_command(tmp_path):
    action = HostAction(id='test-action', title='隔离测试动作', script='printf registered', cwd=str(tmp_path))
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps(ShellPolicy(host_enabled=True, host_members=['admin'], host_actions=[action]).model_dump(mode='json')))
    path.chmod(0o600)
    settings = SimpleNamespace(shell_policy_file=path, state_dir=tmp_path)
    value = host_action(settings, Actor('admin', 'home', 'device', 'infrastructure_owner'), 'test-action')
    assert asyncio.run(run_host(value))['stdout'] == 'registered'


def test_parallel_actions_are_rejected_before_dispatch(tmp_path):
    started = tmp_path / 'started'
    second = tmp_path / 'second'

    async def execute():
        first = asyncio.create_task(run_host(registered(tmp_path, 'touch ' + shlex.quote(str(started)) + '; sleep 0.5')))
        while not started.exists():
            await asyncio.sleep(0.01)
        with pytest.raises(HostExecutionError) as error:
            await run_host(registered(tmp_path, 'touch ' + shlex.quote(str(second))))
        assert not error.value.dispatched and not second.exists()
        assert (await first)['exit_code'] == 0
        assert (await run_host(registered(tmp_path, 'printf next')))['stdout'] == 'next'

    asyncio.run(execute())


@pytest.mark.parametrize('kind', ['directory_symlink', 'file_symlink', 'hardlink', 'file_permissions', 'directory_permissions', 'fifo'])
def test_invalid_lock_path_is_rejected_before_dispatch(tmp_path, private_host_lock, kind):
    root = private_host_lock
    if kind == 'directory_symlink':
        target = tmp_path / 'actual'
        target.mkdir(mode=0o700)
        root.symlink_to(target, target_is_directory=True)
    else:
        root.mkdir(mode=0o700)
        lock = root / 'execution.lock'
        if kind == 'directory_permissions':
            root.chmod(0o755)
        elif kind == 'fifo':
            os.mkfifo(lock, 0o600)
        elif kind in ('file_symlink', 'hardlink'):
            target = tmp_path / 'outside'
            target.write_bytes(b'')
            target.chmod(0o600)
            lock.symlink_to(target) if kind == 'file_symlink' else os.link(target, lock)
        elif kind == 'file_permissions':
            lock.write_bytes(b'')
            lock.chmod(0o644)
    with pytest.raises(HostExecutionError) as error:
        asyncio.run(run_host(registered(tmp_path, 'printf must-not-run')))
    assert not error.value.dispatched


def test_cross_process_lock_blocks_and_is_released(tmp_path, private_host_lock):
    descriptor = shell_host._acquire_lock()
    script = "import fcntl,os,sys; f=os.open(sys.argv[1],os.O_RDWR); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
    try:
        result = subprocess.run([sys.executable, '-I', '-c', script, str(private_host_lock / 'execution.lock')],
                                capture_output=True, timeout=5)
        assert result.returncode != 0 and b'BlockingIOError' in result.stderr
    finally:
        os.close(descriptor)
    result = subprocess.run([sys.executable, '-I', '-c', script, str(private_host_lock / 'execution.lock')],
                            capture_output=True, timeout=5)
    assert result.returncode == 0


def test_child_keeps_lock_after_core_process_exits(tmp_path, private_host_lock):
    # 专用子进程模拟 Core 崩溃；仅执行临时目录中的 sleep，避免任何业务副作用。
    source = str(__import__('pathlib').Path(shell_host.__file__).resolve().parents[1])
    code = r"""
import asyncio,json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from homeai import shell_host
shell_host._lock_root=lambda:Path(sys.argv[2])
async def main():
    lock=shell_host._acquire_lock()
    child=await asyncio.create_subprocess_exec('/bin/sh','-c','sleep 15',pass_fds=(lock,),start_new_session=True)
    print(child.pid,flush=True)
    os._exit(0)
asyncio.run(main())
"""
    parent = subprocess.Popen([sys.executable, '-I', '-c', code, source, str(private_host_lock)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    child_pid = None
    try:
        child_pid = int(parent.stdout.readline().strip())
        assert parent.wait(timeout=5) == 0
        with pytest.raises(HostExecutionError) as error:
            shell_host._acquire_lock()
        assert not error.value.dispatched
    finally:
        if child_pid is not None:
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=5)
        parent.stdout.close()
        parent.stderr.close()
    # macOS/Linux 异步回收进程后，继承锁才能释放。
    for attempt in range(100):
        try:
            lock = shell_host._acquire_lock()
            os.close(lock)
            break
        except HostExecutionError:
            if attempt == 99:
                raise
            time.sleep(0.01)


def test_execution_lock_is_passed_to_actual_child(tmp_path, private_host_lock, monkeypatch):
    original_spawn = asyncio.create_subprocess_exec
    inherited = []

    async def checked_spawn(*args, **kwargs):
        inherited.extend(kwargs.get('pass_fds', ()))
        assert len(inherited) == 1
        assert os.fstat(inherited[0]).st_ino == (private_host_lock / 'execution.lock').stat().st_ino
        return await original_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', checked_spawn)
    assert asyncio.run(run_host(registered(tmp_path, 'printf inherited')))['stdout'] == 'inherited'
    with pytest.raises(OSError):
        os.fstat(inherited[0])


def test_lock_directory_owned_by_another_account_is_rejected(tmp_path, monkeypatch):
    account = os.getuid()
    monkeypatch.setattr(shell_host.os, 'getuid', lambda: account + 1)
    with pytest.raises(HostExecutionError) as error:
        asyncio.run(run_host(registered(tmp_path, 'printf must-not-run')))
    assert not error.value.dispatched
