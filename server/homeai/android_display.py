"""实体屏幕电源只属于当前人工会话；不发送锁屏、唤醒或解锁按键。"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat
import time

from fastapi import HTTPException

from . import android_adb, android_workspace


ERRORS = {
    'device_locked': '安卓已锁屏，请先在安卓端解锁；不会绕过锁屏',
    'device_not_interactive': '安卓已休眠，请先在安卓端唤醒并解锁',
    'unsupported': '当前系统不支持关闭实体屏幕后继续投屏',
    'restore_failed': '实体屏幕恢复结果未确认，请在安卓端检查',
}


def display_block_reason(settings, serial):
    """本机实际兼容性失败记录只禁用熄屏，不影响无线、投屏和恢复操作。"""
    path = Path(settings.state_dir) / 'android-display-blocked.json'
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None if getattr(settings, 'android_display_control_enabled', False) else '实体熄屏尚未启用，连接和投屏不受影响'
    except OSError:
        return '本机实体屏幕兼容性记录不可用，已暂停熄屏'
    try:
        with os.fdopen(descriptor, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError()
            raw = source.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        record = json.loads(raw)
        value = record.get('devices', {}).get(hashlib.sha256(serial.encode()).hexdigest())
        if value is None:
            return None if getattr(settings, 'android_display_control_enabled', False) else '实体熄屏尚未启用，连接和投屏不受影响'
        if not isinstance(value, str) or not 1 <= len(value) <= 300:
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError, AttributeError):
        return '本机实体屏幕兼容性记录无效，已暂停熄屏'


class DisplayBridge:
    def __init__(self, process):
        self.process = process
        self.lock = asyncio.Lock()
        self.sequence = 0
        self.last = None
        self.closed = False
        self.close_task = None
        self.may_need_restore = False
        self.drain = asyncio.create_task(self._drain())

    async def _drain(self):
        while await self.process.stderr.read(4096):
            pass

    async def _reply(self, identifier):
        raw = await asyncio.wait_for(self.process.stdout.readline(), 2)
        try:
            if not raw or len(raw) > 8192:
                raise ValueError()
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get('id') != identifier:
                raise ValueError()
            if any(type(data.get(key)) is not bool for key in ('ok', 'supported', 'requested_off', 'physical_verified')):
                raise ValueError()
            if data.get('physical_off') is not None and type(data['physical_off']) is not bool:
                raise ValueError()
            self.last = data
            return data
        except ValueError:
            raise HTTPException(503, '实体屏幕控制未返回有效状态，请重新连接') from None

    async def send(self, action):
        if action not in {'status', 'off', 'on', 'keepalive'}:
            raise ValueError('不支持的实体屏幕动作')
        async with self.lock:
            if self.closed or self.process.returncode is not None:
                raise HTTPException(409, '实体屏幕控制会话已关闭')
            async def exchange():
                self.sequence += 1
                if action == 'off':
                    self.may_need_restore = True
                raw = json.dumps({'id': self.sequence, 'action': action}, separators=(',', ':')).encode() + b'\n'
                self.process.stdin.write(raw)
                await asyncio.wait_for(self.process.stdin.drain(), 1)
                result = await self._reply(self.sequence)
                self.may_need_restore = result['requested_off'] or bool(result.get('restore_pending'))
                return result
            task = asyncio.create_task(exchange())
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # 消费本次回执再释放锁，避免下一次“恢复”读到遗留的关屏回执。
                await asyncio.gather(task, return_exceptions=True)
                raise

    async def close(self):
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close())
        await asyncio.shield(self.close_task)

    async def _close(self):
        failure = None
        try:
            if self.process.returncode is None and self.may_need_restore:
                result = await self.send('on')
                if result.get('restore_pending') or not result['ok']:
                    failure = HTTPException(503, '实体屏幕恢复结果未确认，请在安卓端检查')
        except Exception:
            failure = HTTPException(503, '实体屏幕控制已断开，等待设备恢复原显示状态')
        finally:
            self.closed = True
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except asyncio.TimeoutError:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 1)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await asyncio.wait_for(self.process.wait(), 1)
            finally:
                self.drain.cancel()
                await asyncio.gather(self.drain, return_exceptions=True)
        if failure:
            raise failure


async def create_display_bridge(settings, serial):
    policy = android_adb.load_policy(settings)
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
        raise HTTPException(409, '请先在家庭服务器构建最新安卓控制组件') from None
    digest = hashlib.sha256(raw).hexdigest()
    remote = '/data/local/tmp/homeai-display-' + digest + '.jar'
    # 固定在同一个已核验的 ADB transport 上，不在一条指令中自动改走其他设备。
    prefix = await android_adb.command_prefix(policy, serial)
    await android_adb._execute_argv(prefix + ['push', str(path), remote], timeout=15)
    checksum = await android_adb._execute_argv(prefix + ['shell', 'sha256sum', remote])
    if not checksum.decode().split() or checksum.decode().split()[0] != digest:
        raise HTTPException(409, '设备端屏幕控制组件校验失败')
    environment = {key: os.environ[key] for key in ('HOME', 'USER', 'TMPDIR') if key in os.environ}
    environment['PATH'] = '/usr/bin:/bin'
    process = await asyncio.create_subprocess_exec(*prefix, 'shell', 'CLASSPATH=' + remote,
        'app_process', '/', 'dev.homeai.androidinput.PowerServer',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=environment, limit=16384)
    bridge = DisplayBridge(process)
    try:
        response = await bridge._reply(0)
        if response.get('ready') is not True or response.get('protocol') != 1:
            raise HTTPException(503, '实体屏幕控制组件尚未就绪')
        return bridge
    except BaseException:
        try:
            await bridge.close()
        except Exception:
            pass
        raise


class DisplayGuard:
    def __init__(self, manager, actor, workspace_id, serial):
        self.manager, self.actor, self.workspace_id, self.serial = manager, actor, workspace_id, serial
        self.lock = asyncio.Lock()
        self.bridge = None
        self.streams = set()
        self.owner = None
        self.epoch = None
        self.last_check = 0.0
        self.revision = 0
        self.blocked_reason = None
        self.state = {'display_control_supported': False, 'device_screen_off': None,
                      'display_verified': False, 'display_reason': '正在检查实体屏幕控制能力'}

    def snapshot(self):
        blocked = display_block_reason(self.manager.app.settings, self.serial)
        if blocked != self.blocked_reason:
            self.blocked_reason = blocked
            self.revision += 1
        result = {**self.state, 'display_revision': self.revision}
        if blocked:
            result.update(display_control_supported=False, display_reason=blocked)
        return result

    def update(self, response):
        reason = response.get('error')
        if reason:
            reason = ERRORS.get(reason, '实体屏幕操作未完成，请检查安卓状态')
        elif not response['supported']:
            reason = ERRORS['unsupported']
        elif response.get('restore_pending') and not response['requested_off']:
            reason = ERRORS['restore_failed']
        elif response['requested_off'] and not response['physical_verified']:
            reason = '已请求关闭实体屏幕；远程画面保持播放'
        else:
            reason = None
        state = {'display_control_supported': response['supported'], 'device_screen_off': response['requested_off'],
                 'display_verified': response['physical_verified'], 'display_reason': reason}
        if state != self.state:
            self.revision += 1
            self.state = state

    def unavailable(self, error):
        self.revision += 1
        self.state = {'display_control_supported': False, 'device_screen_off': None,
                      'display_verified': False, 'display_reason': getattr(error, 'detail', '实体屏幕状态暂不可用')}

    async def attach(self, identifier):
        async with self.lock:
            self.streams.add(identifier)
            blocked = display_block_reason(self.manager.app.settings, self.serial)
            if blocked and self.bridge is None:
                # 已确认不启用熄屏的设备只传画面，不能在探测或退出时刷新实体亮度。
                self.unavailable(HTTPException(409, blocked))
                return self.snapshot()
            if self.bridge is None:
                try:
                    self.bridge = await create_display_bridge(self.manager.app.settings, self.serial)
                    self.update(self.bridge.last)
                except Exception as error:
                    self.unavailable(error)
            return self.snapshot()

    def eligible(self, identifier, actor, epoch):
        entry = self.manager.entries.get(identifier)
        if (identifier not in self.streams or not entry or entry['device_id'] != actor.device_id
                or entry['owner_id'] != actor.user_id):
            raise HTTPException(404, '屏幕会话已关闭')
        with self.manager.app.db() as db:
            android_workspace._active_device(db, actor)
            row = android_workspace._required(self.manager.app, db, actor)
            android_workspace._expire(db, row)
            allowed = (row.id == self.workspace_id and row.serial == self.serial and row.mode == 'human'
                and row.controller_device_id == actor.device_id and row.epoch == epoch)
            db.commit()
            if not allowed:
                raise HTTPException(409, '只有当前接管设备可以控制安卓实体屏幕')
        if not entry.get('touch_ready') or entry.get('touch_live_until', 0) <= time.monotonic():
            raise HTTPException(409, '操作连接尚未就绪或已中断，实体屏幕保持正常显示')

    async def _restore(self):
        if self.bridge:
            try:
                response = await self.bridge.send('on')
                self.update(response)
                if not response['ok'] or response.get('restore_pending'):
                    raise HTTPException(503, ERRORS['restore_failed'])
            except Exception as error:
                self.unavailable(error)
                bridge, self.bridge = self.bridge, None
                if bridge:
                    try:
                        await bridge.close()
                    except Exception:
                        pass
                self.owner, self.epoch = None, None
                raise
        self.owner, self.epoch = None, None

    async def set_off(self, identifier, actor, epoch, value):
        async with self.lock:
            self.eligible(identifier, actor, epoch)
            if value:
                blocked = display_block_reason(self.manager.app.settings, self.serial)
                if blocked:
                    raise HTTPException(409, blocked)
            if not self.bridge or not self.state['display_control_supported']:
                raise HTTPException(409, self.state['display_reason'] or ERRORS['unsupported'])
            if self.owner is not None and self.owner != identifier:
                await self._restore()
            try:
                response = await self.bridge.send('off' if value else 'on')
            except BaseException:
                # 请求取消也可能已经熄屏，必须先承担恢复责任，不能继续给未知状态续租。
                restore = asyncio.create_task(self._restore())
                try:
                    await asyncio.shield(restore)
                except Exception:
                    pass
                raise
            self.update(response)
            if not response['ok']:
                raise HTTPException(409, self.state['display_reason'] or '实体屏幕操作被拒绝')
            # 等待设备回执时可能退出、撤权或换主，迟到的关屏必须立即恢复。
            try:
                self.eligible(identifier, actor, epoch)
            except Exception:
                await self._restore()
                raise
            self.owner, self.epoch = (identifier, epoch) if value else (None, None)
            self.last_check = time.monotonic()
            return self.snapshot()

    async def tick(self):
        async with self.lock:
            if not self.bridge or time.monotonic() - self.last_check < 1:
                return self.snapshot()
            self.last_check = time.monotonic()
            try:
                if self.owner:
                    entry = self.manager.entries.get(self.owner)
                    if not entry:
                        await self._restore()
                    else:
                        actor = entry['media'].actor
                        try:
                            self.eligible(self.owner, actor, self.epoch)
                        except HTTPException:
                            await self._restore()
                response = await self.bridge.send('keepalive' if self.owner else 'status')
                self.update(response)
                if not response['requested_off']:
                    self.owner, self.epoch = None, None
            except Exception as error:
                self.unavailable(error)
            return self.snapshot()

    async def restore_for(self, identifier):
        async with self.lock:
            if self.owner == identifier:
                try:
                    await self._restore()
                except Exception:
                    pass  # 设备独立租约仍会恢复，保留未知状态而不是报成功。

    async def detach(self, identifier):
        async with self.lock:
            if self.owner == identifier:
                try:
                    await self._restore()
                except Exception:
                    pass
            self.streams.discard(identifier)
            if not self.streams and self.bridge:
                bridge, self.bridge = self.bridge, None
                try:
                    await bridge.close()
                except Exception as error:
                    self.unavailable(error)


def guard_for(manager, actor, workspace_id, serial):
    if not hasattr(manager, 'display_guards'):
        manager.display_guards = {}
    key = (actor.household_id, workspace_id)
    guard = manager.display_guards.get(key)
    if guard is None:
        guard = DisplayGuard(manager, actor, workspace_id, serial)
        manager.display_guards[key] = guard
    return guard
