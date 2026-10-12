"""真人触控透传：连续手势持有独立设备锁，接管与撤销必须等待触点释放。"""
import asyncio
import fcntl
import hashlib
import inspect
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import UUID
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator


class GesturePoint(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: int = Field(ge=0, le=9)
    x: float = Field(ge=0, le=1, allow_inf_nan=False)
    y: float = Field(ge=0, le=1, allow_inf_nan=False)


class GesturePacket(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    gesture_id: str = Field(min_length=36, max_length=36)
    epoch: int = Field(ge=1)
    sequence: int = Field(ge=1)
    frame_id: str | None = Field(default=None, min_length=16, max_length=128)
    phase: Literal['down', 'pointer_down', 'move', 'pointer_up', 'up', 'cancel', 'keepalive']
    changed_id: int | None = Field(default=None, ge=0, le=9)
    points: list[GesturePoint] = Field(default_factory=list, max_length=5)

    @model_validator(mode='after')
    def valid_shape(self):
        try:
            UUID(self.gesture_id)
        except ValueError:
            raise ValueError('手势标识必须是 UUID') from None
        if len({point.id for point in self.points}) != len(self.points):
            raise ValueError('触点标识不能重复')
        if self.phase == 'down' and (len(self.points) != 1 or not self.frame_id):
            raise ValueError('首个触点必须绑定当前画面')
        if self.phase in ('down', 'pointer_down', 'pointer_up', 'up'):
            if self.changed_id is None or self.changed_id not in {point.id for point in self.points}:
                raise ValueError('触点变化必须指明对应标识')
        elif self.changed_id is not None:
            raise ValueError('当前事件不应包含变化触点标识')
        if self.phase in ('cancel', 'keepalive') and self.points:
            raise ValueError('取消与保活不能携带输入坐标')
        return self


def _paths(app, actor, member_id=None):
    key = actor.household_id + ':' + (member_id or actor.user_id)
    directory = Path(app.settings.state_dir) / 'android-gestures' / hashlib.sha256(key.encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def _open_lock(directory):
    return os.open(directory / 'gesture.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)


def _markers(directory):
    # 崩溃遗留的取消请求只用于短期围栏；仍在持锁的控制操作受 flock 本身保护。
    pending = []
    for marker in directory.glob('cancel-*'):
        try:
            if time.time() - marker.stat().st_mtime > 30:
                marker.unlink(missing_ok=True)
            else:
                pending.append(marker)
        except FileNotFoundError:
            pass
    return pending


async def _orphan_quiet_period(directory):
    path = directory / 'active'
    if path.exists():
        # 锁已经空闲而标记仍在，前进程可能崩溃；等桥的 1.5 秒触控 watchdog 释放。
        await asyncio.sleep(1.6)
        path.unlink(missing_ok=True)


@asynccontextmanager
async def gesture_barrier(app, actor, *, member_id=None):
    """写取消标记并等实际持锁者释放；超时拒绝接管，不假报已获得操作权。"""
    directory = _paths(app, actor, member_id)
    marker = directory / ('cancel-' + secrets.token_hex(12))
    fd = _open_lock(directory)
    marker.touch(mode=0o600)
    acquired = False
    deadline = time.monotonic() + 2
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise HTTPException(409, 'Android 手势尚未释放，暂时不能切换操作权')
                await asyncio.sleep(0.025)
        try:
            await asyncio.wait_for(_orphan_quiet_period(directory), max(0.001, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            raise HTTPException(409, '旧触控执行器仍在释放触点，暂时不能切换操作权') from None
        yield
    finally:
        marker.unlink(missing_ok=True)
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class GestureSession:
    """每条已鉴权触控通道一个会话；输入来源固定为真人，模型不能调用它。"""
    def __init__(self, app, actor, bridge_factory, on_cancel=None):
        self.app, self.actor, self.bridge_factory = app, actor, bridge_factory
        self.on_cancel = on_cancel
        self.directory = _paths(app, actor)
        self.mutex = asyncio.Lock()
        self.fd = None
        self.bridge = None
        self.bridge_serial = None
        self.bridge_scope = None
        self.workspace_id = None
        self.gesture_audited = False
        self.gesture_id = None
        self.epoch = 0
        self.sequence = 0
        self.points = {}
        self.geometry = None
        self.closed = False
        self.pending = 0
        self.cancellation_generation = 0
        self.watchdog = None
        self.last_client = 0.0
        self.last_bridge = 0.0
        self.rate_window = time.monotonic()
        self.rate_count = 0
        self.last_reason = None
        self.recent_gestures = []

    def _eligible(self, expected_epoch):
        from .android_workspace import _active_device, _required, _expire
        with self.app.db() as db:
            _active_device(db, self.actor)
            row = _required(self.app, db, self.actor)
            _expire(db, row)
            if row.epoch != expected_epoch or row.mode != 'human' or row.controller_device_id != self.actor.device_id:
                db.commit()
                raise HTTPException(409, '触控操作权已变化或过期')
            db.commit()
            return row

    def _bound(self):
        from .android_workspace import _active_device, _required
        with self.app.db() as db:
            _active_device(db, self.actor)
            return _required(self.app, db, self.actor)

    async def _ensure_bridge(self, row):
        from .android_adb import load_policy
        policy = load_policy(self.app.settings)
        if not policy.enabled or row.serial not in policy.serials:
            raise HTTPException(409, 'Android 设备执行授权已撤销')
        identity = (row.id, row.serial, policy.adb_binary)
        process = getattr(self.bridge, 'process', None)
        unusable = getattr(self.bridge, 'closed', False) or (process is not None and process.returncode is not None)
        if self.bridge is not None and (self.bridge_scope != identity or unusable):
            bridge, self.bridge = self.bridge, None
            self.bridge_scope = None
            await asyncio.wait_for(bridge.close(), 1)
        if self.bridge is None:
            factory = self.bridge_factory(self.app.settings, row.serial)
            self.bridge = await asyncio.wait_for(factory, 3) if inspect.isawaitable(factory) else factory
            self.bridge_serial, self.bridge_scope = row.serial, identity
        await self._send({'phase': 'keepalive', 'points': []}, establish=True, expected_active=0)

    async def warm(self):
        """连接时预热固定执行桥，仅读几何信息；不注入触点或获取人工操作权。"""
        from .android_workspace import _lock, _member_key
        try:
            async with asyncio.timeout(3):
                async with self.mutex:
                    if self.closed:
                        raise HTTPException(409, '触控通道已关闭')
                    async with _lock(self.app, _member_key(self.actor)):
                        row = self._eligible(self.epoch) if self.gesture_id else self._bound()
                        if self.gesture_id:
                            # 活跃手势不能通过预热重设旋转基准或切换执行设备。
                            from .android_adb import load_policy
                            policy = load_policy(self.app.settings)
                            if self.bridge_scope != (row.id, row.serial, policy.adb_binary):
                                raise HTTPException(409, '触控执行设备已变化')
                            await self._send({'phase': 'keepalive', 'points': []}, expected_active=len(self.points))
                        else:
                            await self._ensure_bridge(row)
                        # 启动 JVM 期间配对或本机配置仍可能被撤销，返回 ready 前再次核对。
                        refreshed = self._bound()
                        from .android_adb import load_policy
                        policy = load_policy(self.app.settings)
                        if self.bridge_scope != (refreshed.id, refreshed.serial, policy.adb_binary):
                            raise HTTPException(409, '触控执行设备配置已变化，请重新连接')
                        return {'width': self.geometry[0], 'height': self.geometry[1], 'rotation': self.geometry[2]}
        except BaseException as error:
            # 预热失败不能留下未知执行进程或宣称触控已就绪。
            async with self.mutex:
                if self.gesture_id:
                    await self._end('触控预热状态变化')
                if self.bridge is not None:
                    bridge, self.bridge = self.bridge, None
                    self.bridge_scope = None
                    try:
                        await asyncio.wait_for(bridge.close(), 1)
                    except Exception:
                        pass
            if isinstance(error, TimeoutError):
                raise HTTPException(504, '触控组件预热超时，请重新连接') from None
            raise

    def _audit(self, action, status):
        from .data import audit
        from .db import scope
        with self.app.db() as db:
            scope(db, self.actor.user_id, self.actor.household_id)
            audit(db, self.actor, action, self.workspace_id, {
                'gesture_id': self.gesture_id, 'epoch': self.epoch,
                'event_count': self.sequence, 'status': status})
            db.commit()

    @staticmethod
    def _geometry(ack):
        if not isinstance(ack, dict):
            raise HTTPException(502, '触控桥未返回设备几何信息')
        dimensions = (ack.get('width'), ack.get('height'), ack.get('rotation'))
        if any(type(value) is not int for value in dimensions) or not (1 <= dimensions[0] <= 8192 and
                1 <= dimensions[1] <= 8192 and 0 <= dimensions[2] <= 3):
            raise HTTPException(502, '触控桥返回的设备几何信息无效')
        return dimensions

    async def _send(self, payload, *, establish=False, expected_active=None):
        self.last_bridge = time.monotonic()
        ack = await asyncio.wait_for(self.bridge.send(payload), 3 if establish else 0.8)
        geometry = self._geometry(ack)
        # 几何回执正常也可能已经被 Java 看门狗取消，必须核对真实存活触点。
        if expected_active is not None and 'active' in ack:
            if type(ack['active']) is not int or not 0 <= ack['active'] <= 5:
                raise HTTPException(502, '触控桥返回的触点数量无效')
            if ack['active'] != expected_active:
                raise HTTPException(409, '设备已释放或改变触点，当前手势已取消')
        if establish:
            self.geometry = geometry
        elif geometry != self.geometry:
            raise HTTPException(409, '设备方向或尺寸变化，当前手势已取消')
        return ack

    async def _begin(self, packet):
        from .android_workspace import accepted_gesture_frame
        if packet.gesture_id in self.recent_gestures:
            raise HTTPException(409, '已完成手势不能重放')
        if packet.sequence != 1:
            raise HTTPException(409, '新手势必须从序号 1 开始')
        if _markers(self.directory):
            raise HTTPException(409, '正在切换设备操作权，请稍后重试')
        fd = _open_lock(self.directory)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise HTTPException(409, '另一触控会话正在操作此设备') from None
        self.fd = fd
        await _orphan_quiet_period(self.directory)
        if _markers(self.directory):
            raise HTTPException(409, '正在切换设备操作权，请稍后重试')
        # 已持有整个手势的独立 flock；控制变更、解绑与旧输入先经过同一围栏。
        # 截图和续租只更新帧／租约，不应让连续 DOWN 等待一次完整的 ADB 截图。
        row = self._eligible(packet.epoch)
        frame = accepted_gesture_frame(row, packet.frame_id, packet.epoch)
        if frame is None:
            raise HTTPException(409, '触控画面已更新，请在新画面上重新开始')
        await self._ensure_bridge(row)
        if self.geometry[:2] != (frame['width'], frame['height']):
            raise HTTPException(409, '设备画面尺寸已变化，请刷新后重新开始')
        self.gesture_id, self.epoch, self.sequence = packet.gesture_id, packet.epoch, 0
        self.points = {}
        self.workspace_id = row.id
        self._audit('android.gesture.begin', 'started')
        self.gesture_audited = True
        # 只保存进程与时间元数据，不能写触点、屏幕、输入文字或账号信息。
        descriptor = os.open(self.directory / 'active', os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump({'pid': os.getpid(), 'created_at': time.time()}, stream)
        self.last_client = time.monotonic()

    def _transition(self, packet):
        incoming = {point.id: point for point in packet.points}
        old, new = set(self.points), set(incoming)
        phase, changed = packet.phase, packet.changed_id
        if phase == 'down':
            valid = not old and len(new) == 1
            following = incoming
        elif phase == 'pointer_down':
            valid = bool(old) and len(new) <= 5 and changed not in old and new == old | {changed}
            following = incoming
        elif phase == 'move':
            valid = bool(old) and new == old
            following = incoming
        elif phase == 'pointer_up':
            valid = len(old) > 1 and new == old and changed in old
            following = {key: value for key, value in incoming.items() if key != changed}
        elif phase == 'up':
            valid = len(old) == 1 and new == old and changed in old
            following = {}
        elif phase in ('cancel', 'keepalive'):
            valid = bool(old)
            following = {} if phase == 'cancel' else self.points
        else:
            valid, following = False, {}
        if not valid:
            raise HTTPException(409, '触点序列不连续，当前手势已取消')
        return following

    async def handle(self, payload):
        if self.closed:
            raise HTTPException(409, '触控通道已关闭')
        generation = self.cancellation_generation
        self.pending += 1
        try:
            if self.pending > 8:
                self.closed = True
                async with self.mutex:
                    await self._end('触控消息过载')
                raise HTTPException(429, '触控消息过载，已取消当前手势')
            async with self.mutex:
                try:
                    if self.closed:
                        raise HTTPException(409, '触控通道已关闭')
                    if generation != self.cancellation_generation:
                        raise HTTPException(409, '连接状态已变化，旧触控消息已停止')
                    if isinstance(payload, (str, bytes)):
                        if len(payload) > 8192:
                            raise HTTPException(413, '触控消息过大')
                        payload = json.loads(payload)
                    packet = GesturePacket.model_validate(payload)
                    clock = time.monotonic()
                    if clock - self.rate_window >= 1:
                        self.rate_window, self.rate_count = clock, 0
                    self.rate_count += 1
                    if self.rate_count > 180:
                        raise HTTPException(429, '触控频率超过限制')
                    if self.gesture_id is None:
                        if packet.phase != 'down':
                            raise HTTPException(409, '没有已开始的手势')
                        await self._begin(packet)
                    # 等待桥或工作区锁期间通道可能已失活，注入前再次核对取消代数。
                    if generation != self.cancellation_generation:
                        raise HTTPException(409, '连接状态已变化，旧触控消息已停止')
                    if packet.gesture_id != self.gesture_id or packet.epoch != self.epoch or packet.sequence != self.sequence + 1:
                        raise HTTPException(409, '手势标识、控制权或序号不连续')
                    if _markers(self.directory):
                        raise HTTPException(409, '设备操作权正在切换')
                    self._eligible(packet.epoch)
                    following = self._transition(packet)
                    width, height, _ = self.geometry
                    await self._send({'phase': packet.phase, 'changed_id': packet.changed_id,
                        'points': [{'id': point.id, 'x_px': round(point.x * (width - 1)),
                                    'y_px': round(point.y * (height - 1))} for point in packet.points]}, expected_active=len(following))
                    self.points, self.sequence, self.last_client = following, packet.sequence, time.monotonic()
                    result = {'status': 'canceled' if packet.phase == 'cancel' else 'accepted', 'gesture_id': packet.gesture_id,
                              'epoch': packet.epoch, 'sequence': packet.sequence}
                    if packet.phase in ('up', 'cancel'):
                        await self._end(None, completed=packet.phase == 'up')
                    elif self.watchdog is None:
                        self.watchdog = asyncio.create_task(self._watch())
                    return result
                except BaseException as error:
                    await self._end(getattr(error, 'detail', '触控输入无效或连接中断'))
                    if isinstance(error, (ValueError, TypeError)):
                        raise HTTPException(422, '触控数据不符合契约') from None
                    raise
        finally:
            self.pending -= 1

    async def _watch(self):
        try:
            while not self.closed and self.gesture_id is not None:
                await asyncio.sleep(0.1)
                async with self.mutex:
                    if self.gesture_id is None:
                        return
                    if _markers(self.directory):
                        raise HTTPException(409, '设备操作权正在切换')
                    self._eligible(self.epoch)
                    if time.monotonic() - self.last_client > 1.5:
                        raise HTTPException(409, '客户端触控心跳已中断')
                    if time.monotonic() - self.last_bridge >= 0.3:
                        await self._send({'phase': 'keepalive', 'points': []}, expected_active=len(self.points))
        except asyncio.CancelledError:
            return
        except Exception as error:
            async with self.mutex:
                await self._end(getattr(error, 'detail', '触控状态核验失败'))

    async def _end(self, reason, *, completed=False):
        task, self.watchdog = self.watchdog, None
        if task and task is not asyncio.current_task():
            task.cancel()
        self.last_reason = reason
        completed_gesture_id = self.gesture_id
        can_notify_cancel = bool(completed_gesture_id and self.sequence >= 1)
        # 首次 DOWN 尚未被接受时由通道返回 rejected(seq=1)，不能发送非法 canceled(seq=0)。
        cancellation = {'status': 'canceled', 'gesture_id': completed_gesture_id, 'epoch': self.epoch,
                        'sequence': self.sequence, 'reason': reason} if reason and can_notify_cancel else None
        if self.bridge is not None and self.fd is not None:
            try:
                # 正常手势结束仅释放触点，保留同一触控通道的 JVM，双击不用重启执行器。
                ack = await asyncio.wait_for(self.bridge.send({'phase': 'cancel', 'points': []}), 0.8)
                if isinstance(ack, dict) and 'active' in ack and (type(ack['active']) is not int or ack['active'] != 0):
                    raise HTTPException(502, '触控桥尚未确认所有触点释放')
            except BaseException:
                bridge, self.bridge = self.bridge, None
                try:
                    await asyncio.wait_for(bridge.close(), 0.5)
                except BaseException:
                    pass
                await asyncio.sleep(max(0, self.last_bridge + 1.6 - time.monotonic()))
        # 审计失败也必须先释放触点与文件锁；不能因日志写入问题卡住真人接管。
        audit_failure = None
        if self.gesture_id:
            self.recent_gestures = (self.recent_gestures + [self.gesture_id])[-128:]
            if self.gesture_audited:
                self.gesture_audited = False
                try:
                    self._audit('android.gesture.end', 'completed' if completed else 'canceled')
                except Exception as error:
                    audit_failure = error
        self.gesture_id, self.points, self.geometry = None, {}, None
        if self.fd is not None:
            self.directory.joinpath('active').unlink(missing_ok=True)
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
        if audit_failure is not None:
            self.closed = True
            self.last_reason = '触控审计写入失败，通道已停止'
            if cancellation is None and can_notify_cancel:
                cancellation = {'status': 'canceled', 'gesture_id': completed_gesture_id,
                                'epoch': self.epoch, 'sequence': self.sequence, 'reason': self.last_reason}
        if cancellation and self.on_cancel is not None:
            try:
                result = self.on_cancel(cancellation)
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, 0.3)
            except Exception:
                # 通道可能已经断开；取消是否成功以触点释放和设备锁为准。
                pass

    async def cancel(self, reason='连接状态已变化'):
        """断线立即释放触点；通道恢复后可重新预热，不能重放旧的排队事件。"""
        self.cancellation_generation += 1
        async with self.mutex:
            await self._end(reason)

    async def close(self):
        self.cancellation_generation += 1
        self.closed = True
        async with self.mutex:
            await self._end('触控通道已关闭')
            if self.bridge is not None:
                bridge, self.bridge = self.bridge, None
                await asyncio.wait_for(bridge.close(), 1)
