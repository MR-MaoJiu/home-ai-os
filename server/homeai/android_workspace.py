"""成员专属 Android 工作区、人工接管与持久化输入去重。"""
import asyncio
import fcntl
import hashlib
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator
from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column
from . import android_adb
from .client_actions import principal_scope
from .data import audit, emit
from .db import Base, Owned, Principal, Device, now, scope
from .security import authenticate, owner

LEASE_SECONDS = 15
FRAME_SECONDS = 10
router = APIRouter(prefix='/api/v1', tags=['成员 Android 工作区'])


class AndroidWorkspace(Owned, Base):
    __tablename__ = 'android_workspaces'
    __table_args__ = (UniqueConstraint('owner_id'), UniqueConstraint('serial'))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    serial: Mapped[str] = mapped_column(String)
    label: Mapped[str] = mapped_column(String)
    backend: Mapped[str] = mapped_column(String)
    packages: Mapped[str] = mapped_column(Text, default='[]')
    mode: Mapped[str] = mapped_column(String, default='paused')
    active_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    epoch: Mapped[int] = mapped_column(Integer, default=1)
    controller_device_id: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_until: Mapped[float] = mapped_column(default=0)
    frame_id: Mapped[str | None] = mapped_column(String, nullable=True)
    frame_digest: Mapped[str | None] = mapped_column(String, nullable=True)
    frame_width: Mapped[int] = mapped_column(Integer, default=0)
    frame_height: Mapped[int] = mapped_column(Integer, default=0)
    frame_at: Mapped[float] = mapped_column(default=0)
    recent_frames: Mapped[str] = mapped_column(Text, default='[]')
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[float] = mapped_column(default=now)


class AndroidInput(Owned, Base):
    __tablename__ = 'android_inputs'
    __table_args__ = (UniqueConstraint('workspace_id', 'epoch', 'sequence'),)
    workspace_id: Mapped[str] = mapped_column(String, ForeignKey('android_workspaces.id', ondelete='CASCADE'), index=True)
    epoch: Mapped[int] = mapped_column(Integer)
    sequence: Mapped[int] = mapped_column(Integer)
    request_hash: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default='PENDING')
    created_at: Mapped[float] = mapped_column(default=now)



class AndroidInputFocus(Owned, Base):
    __tablename__ = 'android_input_focus'
    workspace_id: Mapped[str] = mapped_column(String, ForeignKey('android_workspaces.id', ondelete='CASCADE'), index=True)
    device_id: Mapped[str] = mapped_column(String)
    epoch: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String)
    fingerprint: Mapped[str] = mapped_column(String)
    expires_at: Mapped[float]
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)

class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class ControlRequest(Contract):
    mode: Literal['paused', 'human', 'ai']
    expected_epoch: int = Field(ge=1)


class InputRequest(Contract):
    epoch: int = Field(ge=1)
    sequence: int = Field(ge=1)
    frame_id: str = Field(min_length=16, max_length=128)
    focus_token: str | None = Field(default=None, min_length=16, max_length=128)
    action: Literal['tap', 'swipe', 'back', 'home', 'text', 'launch']
    x: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    y: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    x2: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    y2: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    text: str | None = Field(default=None, min_length=1, max_length=500)
    package: str | None = Field(default=None, max_length=200)

    @model_validator(mode='after')
    def shape(self):
        expected = {'tap': {'x', 'y'}, 'swipe': {'x', 'y', 'x2', 'y2'},
                    'text': {'text'}, 'launch': {'package'}}.get(self.action, set())
        supplied = {field for field in ('x', 'y', 'x2', 'y2', 'text', 'package') if getattr(self, field) is not None}
        if supplied != expected:
            raise ValueError('Android 动作参数与类型不匹配')
        if self.focus_token is not None and self.action != 'text':
            raise ValueError('只有文字输入可以附带焦点令牌')
        if self.text and ('\0' in self.text or any(ord(c) < 32 for c in self.text)):
            raise ValueError('输入内容不能包含控制字符')
        if self.package and not android_adb.PACKAGE.fullmatch(self.package):
            raise ValueError('Android 应用包名不合法')
        return self



class InputFocusRequest(Contract):
    epoch: int = Field(ge=1)

class BindingRequest(Contract):
    serial: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=100)
    packages: list[str] = Field(default_factory=list, max_length=100)

    @field_validator('serial')
    @classmethod
    def serial_value(cls, value):
        if not android_adb.SERIAL.fullmatch(value):
            raise ValueError('Android 设备序号不合法')
        return value

    @field_validator('packages')
    @classmethod
    def valid_packages(cls, values):
        return normalize_packages(values)


def normalize_packages(values):
    if not isinstance(values, list) or len(values) > 100 or any(not isinstance(item, str) for item in values):
        raise ValueError('AI 应用范围必须是包名列表')
    cleaned = list(dict.fromkeys(item.strip() for item in values))
    if '*' in cleaned:
        if len(cleaned) != 1:
            raise ValueError('允许全部应用时只能填写单独的 *')
        return ['*']
    if any(not android_adb.PACKAGE.fullmatch(item) for item in cleaned):
        raise ValueError('Android 应用包名不合法')
    return cleaned


def ai_packages(row):
    try:
        return normalize_packages(json.loads(row.packages))
    except (ValueError, TypeError):
        raise HTTPException(503, 'AI 应用范围配置无效，请在成员设备中重新设置') from None


async def foreground_package(settings, serial):
    import re
    raw = await android_adb._run(android_adb.load_policy(settings), serial,
        ['shell', 'dumpsys', 'window'], limit=2 * 1024 * 1024)
    match = re.search(rb'mCurrentFocus=Window\{[^}]*?\s(?:u\d+\s+)?([A-Za-z][A-Za-z0-9_.]+)/[^}\s]+', raw)
    if match is None:
        raise HTTPException(409, '当前安卓输入窗口无法可靠确认，请本人接管')
    value = match.group(1).decode('ascii')
    if not android_adb.PACKAGE.fullmatch(value):
        raise HTTPException(409, '当前安卓应用身份无法可靠确认，请本人接管')
    return value


async def assert_ai_package_allowed(app, actor, row, *, action=None):
    packages = ai_packages(row)
    if not packages:
        raise HTTPException(403, '此设备尚未允许 AI 操作任何 App；本人仍可完整接管')
    if action is not None and action.action == 'launch':
        if packages != ['*'] and action.package not in packages:
            raise HTTPException(403, '目标 App 不在 AI 允许范围内')
        return action.package
    if packages == ['*']:
        return None
    package = await foreground_package(app.settings, row.serial)
    if package not in packages:
        raise HTTPException(403, '当前 App 不在 AI 允许范围内；可打开允许的 App 或由本人接管')
    return package


@asynccontextmanager
async def _lock(app, key):
    """锁覆盖 ADB 执行和提交；接管返回时旧输入已结束，跨 worker 也不会同时点击。"""
    directory = Path(app.settings.state_dir) / 'android-locks'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(directory / (hashlib.sha256(key.encode()).hexdigest() + '.lock'),
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    deadline = asyncio.get_running_loop().time() + 12
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if asyncio.get_running_loop().time() > deadline:
                    raise HTTPException(409, '设备仍在完成上一操作，请稍后重试')
                await asyncio.sleep(0.025)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _member_key(actor):
    return actor.household_id + ':' + actor.user_id


def _active_device(db, actor):
    device = db.get(Device, actor.device_id)
    person = db.get(Principal, actor.user_id)
    if not device or device.revoked or device.user_id != actor.user_id or not person or person.household_id != actor.household_id:
        raise HTTPException(401, '需要本人成员的有效配对设备')


def get_binding(app, db, actor):
    scope(db, actor.user_id, actor.household_id)
    return db.scalar(select(AndroidWorkspace).where(AndroidWorkspace.owner_id == actor.user_id,
        AndroidWorkspace.household_id == actor.household_id, AndroidWorkspace.revoked.is_(False)).execution_options(populate_existing=True))


def _required(app, db, actor):
    binding = get_binding(app, db, actor)
    if binding is None:
        raise HTTPException(404, '尚未绑定个人 Android 执行设备')
    policy = android_adb.load_policy(app.settings)
    if not policy.enabled or binding.serial not in policy.serials:
        raise HTTPException(409, 'Android 执行设备已停用或本机授权已撤销')
    return binding


def _pause(row):
    row.mode, row.controller_device_id, row.lease_until = 'paused', None, 0
    row.active_task_id = None
    row.epoch += 1
    row.sequence = 0
    row.frame_id, row.frame_digest, row.frame_at = None, None, 0
    row.recent_frames = '[]'
    row.updated_at = now()


def _expire(db, row):
    if row.mode == 'human':
        device = db.get(Device, row.controller_device_id) if row.controller_device_id else None
        if row.lease_until <= now() or not device or device.revoked or device.user_id != row.owner_id:
            _pause(row)


async def _view(app, row, listed=None):
    listed = await android_adb.devices(app.settings) if listed is None else listed
    device = next((item for item in listed if item['serial'] == row.serial), {})
    status = device.get('status', 'offline')
    task_status = None
    if row.active_task_id:
        from .db import Task
        with app.db() as db:
            scope(db, row.owner_id, row.household_id)
            task = db.get(Task, row.active_task_id)
            if task and task.owner_id == row.owner_id and task.household_id == row.household_id:
                task_status = task.status
    return {'id': row.id, 'label': row.label, 'backend': row.backend, 'status': status,
            'transport': device.get('transport', 'offline'),
            'wireless_configured': device.get('wireless_configured', False),
            'mode': row.mode, 'epoch': row.epoch, 'controller_device_id': row.controller_device_id,
            'packages': json.loads(row.packages), 'active_task_id': row.active_task_id,
            'active_task_status': task_status, 'lease_until': row.lease_until, 'sequence': row.sequence, 'last_sequence': row.sequence}


async def workspace(app, actor):
    async with _lock(app, _member_key(actor)):
        with app.db() as db:
            _active_device(db, actor)
            row = get_binding(app, db, actor)
            if row is None:
                return {'workspace': None}
            _expire(db, row)
            if row.active_task_id:
                from .db import Task
                from .android_tasks import release_finished_in_session
                task = db.get(Task, row.active_task_id)
                if task:
                    release_finished_in_session(app, db, actor, task)
            result = await _view(app, row)
            db.commit()
            return {'workspace': result}



async def control(app, actor, body):
    from .android_gestures import gesture_barrier
    # 同一持有者的续租不取消正在进行的手势。
    if body.mode == 'human':
        async with _lock(app, _member_key(actor)):
            with app.db() as db:
                _active_device(db, actor)
                row = _required(app, db, actor)
                _expire(db, row)
                if row.epoch == body.expected_epoch and row.mode == 'human' and row.controller_device_id == actor.device_id:
                    row.lease_until = now() + LEASE_SECONDS
                    row.updated_at = now()
                    # 先发布租约，不能让 ADB 状态读取阻塞其他进程的手势保活核验。
                    db.commit()
                    result = await _view(app, row)
                    return {'workspace': result}
                db.commit()
    async with gesture_barrier(app, actor):
        return await _change_control(app, actor, body)

async def _change_control(app, actor, body):
    async with _lock(app, _member_key(actor)):
        with app.db() as db:
            _active_device(db, actor)
            row = _required(app, db, actor)
            _expire(db, row)
            if row.epoch != body.expected_epoch:
                db.commit()
                raise HTTPException(409, '操作权已变化，请刷新后重试')
            renewal = body.mode == 'human' and row.mode == 'human' and row.controller_device_id == actor.device_id
            if not renewal:
                previous_task = row.active_task_id
                _pause(row)
                row.mode = body.mode
                row.controller_device_id = actor.device_id if body.mode == 'human' else None
                audit(db, actor, 'android.control.changed', row.id, {'mode': body.mode, 'epoch': row.epoch})
                emit(db, actor, 'android.workspace.updated', row.id)
                if previous_task:
                    from .db import Task, Approval, Invocation
                    previous = db.get(Task, previous_task)
                    if previous and previous.owner_id == actor.user_id and previous.status not in {'SUCCEEDED', 'FAILED', 'CANCELED', 'NEEDS_RECONCILIATION'}:
                        previous.cancel_requested = True
                        if previous.status != 'EXECUTING':
                            previous.status = 'CANCELED'
                        previous.error = '你已接管安卓设备，原 AI 任务已停止；交还设备不会恢复旧操作'
                        calls = select(Invocation.id).where(Invocation.task_id == previous_task)
                        for approval in db.scalars(select(Approval).where(Approval.invocation_id.in_(calls), Approval.decision == 'PENDING')):
                            approval.decision = 'CANCELED'
                    emit(db, actor, 'task.updated', previous_task)
                    emit(db, actor, 'android.task.ended', previous_task)
            row.lease_until = now() + LEASE_SECONDS if body.mode == 'human' else 0
            row.updated_at = now()
            result = await _view(app, row)
            db.commit()
            return {'workspace': result}



def _remember_frame(row):
    clock = now()
    try:
        recent = json.loads(row.recent_frames or '[]')
    except (ValueError, TypeError):
        recent = []
    recent = [item for item in recent if isinstance(item, dict) and item.get('at', 0) >= clock - 3
        and item.get('epoch') == row.epoch and item.get('id') != row.frame_id]
    recent.append({'id': row.frame_id, 'width': row.frame_width, 'height': row.frame_height,
                   'epoch': row.epoch, 'at': clock})
    row.recent_frames = json.dumps(recent[-8:])


def accepted_gesture_frame(row, frame_id, epoch):
    """真人连续手势接受已展示的短期帧；模型坐标点击仍走原严格摘要校验。"""
    try:
        recent = json.loads(row.recent_frames or '[]')
    except (ValueError, TypeError):
        recent = []
    latest = next((item for item in reversed(recent) if isinstance(item, dict) and
        item.get('epoch') == row.epoch and item.get('at', 0) >= now() - 3), {})
    # 连续视频有独立尺寸依据；旧截图仍必须匹配其当前规范尺寸，不能放松原坐标保护。
    dimensions = (latest.get('width'), latest.get('height')) if latest.get('source') == 'stream' else (row.frame_width, row.frame_height)
    return next((item for item in reversed(recent) if isinstance(item, dict) and
        item.get('id') == frame_id and item.get('epoch') == epoch == row.epoch and
        item.get('at', 0) >= now() - 3 and
        (item.get('width'), item.get('height')) == dimensions), None)


async def publish_stream_frame(app, actor, workspace_id, serial, width, height, frame_id):
    """视频只更新真人近期画面凭据，不覆盖 AI 截图的内容摘要和坐标执行依据。"""
    async with _lock(app, _member_key(actor)):
        with app.db() as db:
            _active_device(db, actor)
            row = _required(app, db, actor)
            if row.id != workspace_id or row.serial != serial:
                raise HTTPException(409, '安卓绑定已变化，画面已停止')
            _expire(db, row)
            clock = now()
            try:
                recent = json.loads(row.recent_frames or '[]')
            except (ValueError, TypeError):
                recent = []
            recent = [item for item in recent if isinstance(item, dict) and item.get('at', 0) >= clock - 3
                and item.get('id') != frame_id and item.get('epoch') == row.epoch]
            recent.append({'id': frame_id, 'width': width, 'height': height, 'epoch': row.epoch, 'at': clock, 'source': 'stream'})
            row.recent_frames = json.dumps(recent[-8:])
            db.commit()
            return {'frame_id': frame_id, 'width': width, 'height': height,
                    'epoch': row.epoch, 'mode': row.mode, 'sequence': row.sequence}

async def capture(app, actor, *, source='human'):
    async with _lock(app, _member_key(actor)):
        with app.db() as db:
            _active_device(db, actor)
            row = _required(app, db, actor)
            _expire(db, row)
            if source == 'ai' and row.mode != 'ai':
                db.commit()
                raise HTTPException(409, '成员已接管或暂停，AI 不能观察设备屏幕')
            if source == 'ai':
                await assert_ai_package_allowed(app, actor, row)
            raw, width, height = await android_adb.screenshot(app.settings, row.serial)
            digest = hashlib.sha256(raw).hexdigest()
            if row.frame_digest != digest or row.frame_at + FRAME_SECONDS <= now():
                row.frame_id = secrets.token_urlsafe(24)
            row.frame_digest, row.frame_width, row.frame_height, row.frame_at = digest, width, height, now()
            _remember_frame(row)
            db.commit()
            return raw, {'frame_id': row.frame_id, 'width': width, 'height': height,
                         'epoch': row.epoch, 'mode': row.mode, 'sequence': row.sequence}



async def observe(app, actor):
    """AI 观察在同一个控制锁内完成，成员接管成功后不会继续读取私人登录页面。"""
    async with _lock(app, _member_key(actor)):
        with app.db() as db:
            _active_device(db, actor)
            row = _required(app, db, actor)
            _expire(db, row)
            if row.mode != 'ai':
                db.commit()
                raise HTTPException(409, '成员已接管或暂停，AI 不能观察设备屏幕')
            from .android_action_guard import assert_connected
            await assert_connected(app, row)
            try:
                await assert_ai_package_allowed(app, actor, row)
            except HTTPException as error:
                if error.status_code not in (403, 409):
                    raise
                # 名单外不截图也不读取控件树；模型可用独立启动工具进入允许 App。
                db.commit()
                return b'<hierarchy/>', {'frame_id': None, 'width': 0, 'height': 0,
                    'epoch': row.epoch, 'mode': row.mode, 'sequence': row.sequence,
                    'allowed_packages': ai_packages(row), 'scope_blocked': str(error.detail)}
            xml = await android_adb.hierarchy(app.settings, row.serial)
            await assert_ai_package_allowed(app, actor, row)
            from .android_action_guard import parse, blocking_reason, snapshot, window_fingerprint, AndroidAssistanceRequired
            if blocked := blocking_reason(parse(xml)):
                raise AndroidAssistanceRequired(*blocked)
            raw, width, height = await android_adb.screenshot(app.settings, row.serial)
            digest = hashlib.sha256(raw).hexdigest()
            if row.frame_digest != digest or row.frame_at + FRAME_SECONDS <= now():
                row.frame_id = secrets.token_urlsafe(24)
            row.frame_digest, row.frame_width, row.frame_height, row.frame_at = digest, width, height, now()
            _remember_frame(row)
            focus = {}
            guard_snapshot = snapshot(xml, width, height)
            guard_snapshot['window_fingerprint'] = await window_fingerprint(app.settings, row.serial)
            try:
                signature = await android_adb.focus_signature(app.settings, row.serial, xml=xml)
                if not signature['password']:
                    focus = _focus_claim(db, row, actor, 'ai', signature['fingerprint'])
                    guard_snapshot['focus_fingerprint'] = signature['fingerprint']
            except HTTPException:
                # 页面没有可编辑焦点时仍能观察；不能为未知控件生成输入许可。
                pass
            db.commit()
            return xml, {'frame_id': row.frame_id, 'width': width, 'height': height,
                         'epoch': row.epoch, 'mode': row.mode, 'sequence': row.sequence, **focus,
                         '_guard_snapshot': guard_snapshot,
                         'allowed_packages': ai_packages(row), 'scope_blocked': None}


def _focus_claim(db, row, actor, source, fingerprint):
    # 令牌只保存目标身份摘要，不保存输入框原文；过期记录及时回收。
    db.query(AndroidInputFocus).filter(AndroidInputFocus.owner_id == actor.user_id,
        AndroidInputFocus.household_id == actor.household_id, AndroidInputFocus.expires_at <= now()).delete()
    claim = AndroidInputFocus(id=secrets.token_urlsafe(24), owner_id=actor.user_id,
        household_id=actor.household_id, workspace_id=row.id, device_id=actor.device_id,
        epoch=row.epoch, source=source, fingerprint=fingerprint, expires_at=now() + 30)
    db.add(claim)
    return {'focus_token': claim.id, 'expires_at': claim.expires_at}


async def prepare_input(app, actor, body, *, source='human'):
    if source not in ('human', 'ai'):
        raise HTTPException(422, '输入来源无效')
    async with _lock(app, _member_key(actor)):
        with app.db() as db:
            _active_device(db, actor)
            row = _required(app, db, actor)
            _expire(db, row)
            if row.epoch != body.epoch or row.mode != source or (source == 'human' and row.controller_device_id != actor.device_id):
                db.commit()
                raise HTTPException(409, '当前设备未持有操作权，不能准备文字输入')
            signature = await android_adb.focus_signature(app.settings, row.serial)
            if source == 'ai' and signature['password']:
                raise HTTPException(409, '密码或身份验证输入请由本人接管')
            _expire(db, row)
            if row.epoch != body.epoch:
                db.commit()
                raise HTTPException(409, '操作权已过期，请重新接管后输入')
            result = _focus_claim(db, row, actor, source, signature['fingerprint'])
            db.commit()
            return result



async def launch_for_task(app, actor, package, *, task_id, expected_epoch, before_dispatch):
    if not android_adb.PACKAGE.fullmatch(package):
        raise HTTPException(422, 'Android 应用包名无效')
    from types import SimpleNamespace
    from .android_gestures import gesture_barrier
    async with gesture_barrier(app, actor), _lock(app, _member_key(actor)):
        with app.db() as db:
            _active_device(db, actor)
            row = _required(app, db, actor)
            if row.mode != 'ai' or row.active_task_id != task_id or row.epoch != expected_epoch:
                raise HTTPException(409, 'AI 尚未取得此任务的设备操作权')
            action = SimpleNamespace(action='launch', package=package)
            await assert_ai_package_allowed(app, actor, row, action=action)
            from .android_action_guard import assert_connected
            await assert_connected(app, row)
            installed = await android_adb._run(android_adb.load_policy(app.settings), row.serial,
                ['shell', 'pm', 'path', package], limit=8192)
            if not any(line.startswith(b'package:') for line in installed.splitlines()):
                raise HTTPException(404, '允许列表中的 App 尚未安装在该安卓设备上')
            # 启动不依赖名单外截图或由模型填写的坐标，派发前再核对实时控制权。
            db.refresh(row)
            if row.mode != 'ai' or row.active_task_id != task_id or row.epoch != expected_epoch:
                raise HTTPException(409, '设备已由本人接管，启动操作未派发')
            await assert_ai_package_allowed(app, actor, row, action=action)
            before_dispatch()
            await android_adb.execute(app.settings, row.serial, action)
            confirmed = False
            try:
                confirmed = await foreground_package(app.settings, row.serial) == package
            except HTTPException:
                pass
            audit(db, actor, 'android.app.launch', row.id, {'package': package, 'task_id': row.active_task_id, 'foreground_verified': confirmed})
            db.commit()
            return {'status': 'opened' if confirmed else 'launch_requested', 'foreground_verified': confirmed,
                    'package': package, 'android_workspace_id': row.id,
                    'active_task_id': row.active_task_id,
                    'note': '应用已位于前台；如果目标只是打开App即可结束，不必额外读取页面。后续App内操作先observe_android。' if confirmed else '应用启动已派发但尚未确认前台；不能声称已打开或完成交易，请核对。'}

async def input_event(app, actor, body, *, source='human', before_dispatch=None, assessment=None):
    from .android_gestures import gesture_barrier
    # 不允许过时的 AI 动作通过“取消”打断已经接管的真人手势。
    async with _lock(app, _member_key(actor)):
        with app.db() as db:
            _active_device(db, actor)
            row = _required(app, db, actor)
            _expire(db, row)
            if row.epoch != body.epoch or row.mode != source or (source == 'human' and row.controller_device_id != actor.device_id):
                db.commit()
                raise HTTPException(409, '当前设备未持有操作权，旧输入已停止')
            db.commit()
    async with gesture_barrier(app, actor):
        return await _legacy_input_event(app, actor, body, source=source, before_dispatch=before_dispatch, assessment=assessment)

async def _legacy_input_event(app, actor, body, *, source='human', before_dispatch=None, assessment=None):
    if source not in ('human', 'ai'):
        raise HTTPException(422, '输入来源无效')
    request_hash = hashlib.sha256(json.dumps({'input': body.model_dump(), 'source': source,
        'device_id': actor.device_id}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    async with _lock(app, _member_key(actor)):
        with app.db() as db:
            _active_device(db, actor)
            row = _required(app, db, actor)
            _expire(db, row)
            if row.epoch != body.epoch or row.mode != source or (source == 'human' and row.controller_device_id != actor.device_id):
                db.commit()
                raise HTTPException(409, '当前设备未持有操作权，旧输入已停止')
            existing = db.scalar(select(AndroidInput).where(AndroidInput.workspace_id == row.id,
                AndroidInput.epoch == body.epoch, AndroidInput.sequence == body.sequence))
            if existing:
                if existing.request_hash != request_hash:
                    raise HTTPException(409, '操作序号已用于其他内容')
                if existing.status != 'SUCCEEDED':
                    raise HTTPException(409, '上次操作结果待核对，请查看屏幕后重新操作，不能自动重放')
                return {'status': 'succeeded', 'epoch': body.epoch, 'sequence': body.sequence}
            if body.sequence != row.sequence + 1:
                raise HTTPException(409, '操作序号不连续，请刷新工作区')
            live_navigation = (source == 'human' and body.action in ('back', 'home', 'launch')
                and accepted_gesture_frame(row, body.frame_id, body.epoch) is not None)
            if source == 'human' and body.action != 'text' and not live_navigation and (row.frame_id != body.frame_id or row.frame_at + FRAME_SECONDS <= now()):
                raise HTTPException(409, '设备画面已更新，请等待新画面再操作')
            if source == 'ai':
                await assert_ai_package_allowed(app, actor, row, action=body)
                from .android_action_guard import revalidate
                body = await revalidate(app, db, actor, row, body, assessment)
            if body.action == 'text':
                claim = db.get(AndroidInputFocus, body.focus_token) if body.focus_token else None
                if (not claim or claim.owner_id != actor.user_id or claim.household_id != actor.household_id
                    or claim.workspace_id != row.id or claim.device_id != actor.device_id
                    or claim.epoch != body.epoch or claim.source != source or claim.expires_at <= now() or claim.consumed):
                    raise HTTPException(409, '文字输入焦点已过期或未准备，请重新选择输入框后提交')
                signature = await android_adb.focus_signature(app.settings, row.serial)
                if signature['fingerprint'] != claim.fingerprint or (source == 'ai' and signature['password']):
                    raise HTTPException(409, '输入框或前台窗口已变化，已阻止文字写入其他位置')
                if claim.expires_at <= now():
                    raise HTTPException(409, '文字输入焦点已过期，请重新选择输入框后提交')
            if source == 'human' and body.action in ('tap', 'swipe'):
                raw, width, height = await android_adb.screenshot(app.settings, row.serial)
                if (width, height) != (row.frame_width, row.frame_height) or hashlib.sha256(raw).hexdigest() != row.frame_digest:
                    row.frame_id, row.frame_digest, row.frame_at = None, None, 0
                    db.commit()
                    raise HTTPException(409, '设备画面在点击前发生变化，已阻止旧坐标操作')
            if source == 'ai':
                await assert_ai_package_allowed(app, actor, row, action=body)
            _expire(db, row)
            if row.epoch != body.epoch:
                db.commit()
                raise HTTPException(409, '操作权已过期，旧输入已停止')
            if body.action == 'text':
                claim.consumed = True
            pending = AndroidInput(owner_id=actor.user_id, household_id=actor.household_id, workspace_id=row.id,
                epoch=body.epoch, sequence=body.sequence, request_hash=request_hash, status='PENDING')
            db.add(pending)
            row.sequence = body.sequence
            row.frame_id, row.frame_digest, row.frame_at = None, None, 0
            db.commit()
            # 校验拒绝不代表已派发；只在设备调用前持久化任务派发标记。
            if before_dispatch is not None:
                before_dispatch()
            try:
                await android_adb.execute(app.settings, row.serial, body, width=row.frame_width, height=row.frame_height)
                pending.status = 'SUCCEEDED'
            except BaseException:
                # 已派发的动作可能生效；崩溃或超时都不能盲目重复提交。
                pending.status = 'UNKNOWN'
                db.commit()
                raise
            audit(db, actor, 'android.input.completed', row.id, {'action': body.action, 'epoch': body.epoch,
                'sequence': body.sequence, 'source': source})
            db.commit()
            return {'status': 'succeeded', 'epoch': body.epoch, 'sequence': body.sequence}


@router.get('/android/workspace')
async def read_workspace(request: Request, actor=Depends(authenticate)):
    return await workspace(request.app.state, actor)


@router.post('/android/control')
async def change_control(body: ControlRequest, request: Request, actor=Depends(authenticate)):
    return await control(request.app.state, actor, body)



@router.post('/android/input-focus')
async def prepare_text_input(body: InputFocusRequest, request: Request, actor=Depends(authenticate)):
    return await prepare_input(request.app.state, actor, body)

@router.post('/android/input')
async def send_input(body: InputRequest, request: Request, actor=Depends(authenticate)):
    return await input_event(request.app.state, actor, body)


@router.get('/manage/android/devices')
async def list_devices(request: Request, actor=Depends(authenticate)):
    owner(actor)
    return {'devices': await android_adb.devices(request.app.state.settings)}


async def bindings(app, actor):
    owner(actor)
    listed = await android_adb.devices(app.settings)
    with app.db() as db:
        scope(db, actor.user_id, actor.household_id)
        rows = db.scalars(select(AndroidWorkspace).where(AndroidWorkspace.household_id == actor.household_id, AndroidWorkspace.revoked.is_(False))).all()
        return {'bindings': [{**await _view(app, row, listed), 'member_id': row.owner_id,
             'workspace_id': row.id, 'serial': row.serial} for row in rows]}


@router.get('/manage/android/bindings')
async def list_bindings(request: Request, actor=Depends(authenticate)):
    return await bindings(request.app.state, actor)


@router.put('/manage/android/bindings/{member_id}')
async def bind_device(member_id: str, body: BindingRequest, request: Request, actor=Depends(authenticate)):
    owner(actor)
    app = request.app.state
    from .android_gestures import gesture_barrier
    async with gesture_barrier(app, actor, member_id=member_id), _lock(app, actor.household_id + ':' + member_id):
        with app.db() as db:
            scope(db, actor.user_id, actor.household_id)
            person = db.scalar(select(Principal).where(Principal.id == member_id, Principal.household_id == actor.household_id))
            if person is None:
                raise HTTPException(404, '家庭成员不存在')
            policy = android_adb.load_policy(app.settings)
            if not policy.enabled or body.serial not in policy.serials:
                raise HTTPException(403, '设备尚未由本机管理员登记')
            row = db.scalar(select(AndroidWorkspace).where(AndroidWorkspace.owner_id == member_id,
                AndroidWorkspace.household_id == actor.household_id))
            if row and row.serial != body.serial:
                raise HTTPException(409, '此成员已有专属设备，迁移前需本机完成旧设备账号清理与归属迁移')
            try:
                with principal_scope(db, member_id, actor.household_id):
                    if row is None:
                        row = AndroidWorkspace(owner_id=member_id, household_id=actor.household_id, serial=body.serial,
                            label=body.label, backend='emulator' if body.serial.startswith('emulator-') else 'physical')
                        db.add(row)
                    else:
                        _pause(row)
                    row.revoked = False
                    row.label, row.packages = body.label, json.dumps(body.packages)
                audit(db, actor, 'android.binding.updated', member_id, {'packages': body.packages})
                db.commit()
            except IntegrityError:
                raise HTTPException(409, '该 Android 设备已被其他成员独占绑定') from None
    return await bindings(app, actor)


@router.delete('/manage/android/bindings/{member_id}')
async def unbind_device(member_id: str, request: Request, actor=Depends(authenticate)):
    owner(actor)
    app = request.app.state
    from .android_gestures import gesture_barrier
    async with gesture_barrier(app, actor, member_id=member_id), _lock(app, actor.household_id + ':' + member_id):
        with app.db() as db:
            scope(db, actor.user_id, actor.household_id)
            row = db.scalar(select(AndroidWorkspace).where(AndroidWorkspace.owner_id == member_id,
                AndroidWorkspace.household_id == actor.household_id))
            if row is not None:
                with principal_scope(db, member_id, actor.household_id):
                    _pause(row)
                    # 保留设备归属墓碑，避免注销后另一成员接收未清除的应用账号。
                    row.revoked = True
                audit(db, actor, 'android.binding.deleted', member_id)
                db.commit()
    return {'ok': True}
