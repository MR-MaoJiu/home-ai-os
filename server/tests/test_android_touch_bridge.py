"""触控执行器关闭在外层取消后仍回收资源；仅使用内存进程替身。"""
import asyncio
import json

import pytest
from fastapi import HTTPException

from homeai import android_touch_bridge as touch


class InputPipe:
    def __init__(self, process):
        self.process = process
        self.closed = False
        self.messages = []

    def write(self, raw):
        self.messages.append(json.loads(raw))
        if self.process.broken_pipe:
            raise BrokenPipeError()

    async def drain(self):
        return

    def close(self):
        self.closed = True
        if self.process.exit_on_close:
            self.process.returncode = 0
            self.process.exited.set()


class ReplyPipe:
    async def readline(self):
        return json.dumps({'ok': True, 'id': 1, 'width': 320, 'height': 640, 'rotation': 0}).encode() + b'\n'


class ErrorPipe:
    def __init__(self):
        self.finished = False

    async def read(self, limit):
        try:
            await asyncio.Event().wait()
        finally:
            self.finished = True


class PipeTransport:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class Process:
    def __init__(self, *, exit_on_close=False, broken_pipe=False, exit_on_kill=True):
        self.returncode = None
        self.exit_on_close, self.broken_pipe, self.exit_on_kill = exit_on_close, broken_pipe, exit_on_kill
        self.exited, self.waiting = asyncio.Event(), asyncio.Event()
        self.kills = 0
        self.stdin, self.stdout, self.stderr = InputPipe(self), ReplyPipe(), ErrorPipe()
        self._transport = PipeTransport()

    async def wait(self):
        self.waiting.set()
        await self.exited.wait()
        return self.returncode

    def kill(self):
        self.kills += 1
        if self.exit_on_kill:
            self.returncode = -9
            self.exited.set()


@pytest.fixture(autouse=True)
def short_cleanup_timeouts(monkeypatch):
    monkeypatch.setattr(touch, '_CANCEL_TIMEOUT', 0.05)
    monkeypatch.setattr(touch, '_EXIT_TIMEOUT', 0.05)
    monkeypatch.setattr(touch, '_KILL_TIMEOUT', 0.05)


def assert_released(bridge, process):
    assert bridge.closed and bridge.close_task.done()
    assert process.stdin.closed and process._transport.closed
    assert bridge.errors.done() and process.stderr.finished


async def test_graceful_close_sends_cancel_once_and_repeat_waits_completed_task():
    process = Process(exit_on_close=True)
    bridge = touch.TouchBridge(process)
    await bridge.close()
    cleanup = bridge.close_task
    await bridge.close()
    assert bridge.close_task is cleanup
    assert process.stdin.messages == [{'phase': 'cancel', 'points': [], 'id': 1}]
    assert process.kills == 0
    assert_released(bridge, process)


async def test_caller_cancel_waits_for_process_kill_without_swallowing_cancel():
    process = Process()
    bridge = touch.TouchBridge(process)
    caller = asyncio.create_task(bridge.close())
    await asyncio.wait_for(process.waiting.wait(), 1)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(caller, 1)
    assert process.kills == 1 and process.returncode == -9
    assert_released(bridge, process)
    await bridge.close()
    assert process.kills == 1


async def test_simultaneous_close_callers_share_cleanup_when_one_is_canceled():
    process = Process()
    bridge = touch.TouchBridge(process)
    first = asyncio.create_task(bridge.close())
    await asyncio.wait_for(process.waiting.wait(), 1)
    cleanup = bridge.close_task
    second = asyncio.create_task(bridge.close())
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert bridge.close_task is cleanup
    assert process.kills == 1 and len(process.stdin.messages) == 1
    assert_released(bridge, process)


async def test_outer_timeout_releases_resources_before_propagating_timeout():
    process = Process()
    bridge = touch.TouchBridge(process)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bridge.close(), 0.005)
    assert process.kills == 1
    assert_released(bridge, process)


async def test_cancel_reply_pipe_failure_still_reaps_process():
    process = Process(broken_pipe=True)
    bridge = touch.TouchBridge(process)
    await bridge.close()
    assert process.kills == 1
    assert_released(bridge, process)


async def test_stuck_process_reports_failure_with_bounded_pipe_cleanup():
    process = Process(exit_on_kill=False)
    bridge = touch.TouchBridge(process)
    async with asyncio.timeout(1):
        with pytest.raises(HTTPException) as error:
            await bridge.close()
    assert error.value.status_code == 503 and process.kills == 1
    assert_released(bridge, process)
    with pytest.raises(HTTPException):
        await bridge.close()
    assert process.kills == 1
