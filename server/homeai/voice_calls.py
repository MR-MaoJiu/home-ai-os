"""成员自己的语音通话；模型提出呼叫，服务端决定权限、频率和生命周期。"""
import base64
import io
import json
import wave
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, field_validator
from sqlalchemy import String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from .contracts import Contract, TaskRequest
from .crypto import canonical, digest
from .db import Base, Owned, Device, Principal, Task, now, scope, uid
from .security import authenticate, own
from .data import audit, emit

router = APIRouter(prefix='/api/v1/voice', tags=['语音与通话'])
TERMINAL = {'ENDED', 'DECLINED', 'MISSED', 'FAILED'}


class VoicePreferences(Owned, Base):
    __tablename__ = 'voice_preferences'
    __table_args__ = (UniqueConstraint('owner_id'),)
    payload: Mapped[str] = mapped_column(Text)


class VoIPRegistration(Owned, Base):
    __tablename__ = 'voip_registrations'
    __table_args__ = (UniqueConstraint('device_id'),)
    device_id: Mapped[str] = mapped_column(String)
    token: Mapped[str] = mapped_column(Text)
    environment: Mapped[str] = mapped_column(String)
    updated_at: Mapped[float] = mapped_column(default=now)


class VoiceCall(Owned, Base):
    __tablename__ = 'voice_calls'
    __table_args__ = (UniqueConstraint('owner_id', 'request_key'),)
    request_key: Mapped[str] = mapped_column(String)
    target_device_id: Mapped[str] = mapped_column(String)
    source_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    direction: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    push_status: Mapped[str] = mapped_column(String, default='NONE')
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(default=now)
    expires_at: Mapped[float]
    last_seen: Mapped[float] = mapped_column(default=now)


class VoiceTurn(Owned, Base):
    __tablename__ = 'voice_turns'
    __table_args__ = (UniqueConstraint('call_id', 'client_key'),)
    call_id: Mapped[str] = mapped_column(String, index=True)
    client_key: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default='TRANSCRIBING')
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(default=now)


class Preferences(Contract):
    incoming_enabled: bool = False
    quiet_start: int = Field(default=22, ge=0, le=23)
    quiet_end: int = Field(default=8, ge=0, le=23)
    daily_limit: int = Field(default=4, ge=1, le=8)
    timezone: str = 'Asia/Shanghai'
    speaker: str = Field(default='中文女', min_length=1, max_length=100)

    @field_validator('timezone')
    @classmethod
    def zone(cls, value):
        try: ZoneInfo(value)
        except Exception: raise ValueError('无效时区') from None
        return value


class Registration(Contract):
    token: str = Field(min_length=32, max_length=512, pattern=r'^[0-9a-fA-F]+$')
    environment: Literal['sandbox', 'production']


class Start(Contract):
    client_key: str = Field(min_length=8, max_length=100)


class AudioInput(Start):
    content_base64: str = Field(min_length=1, max_length=2800000)


def preferences(app, db, actor):
    row = db.scalar(select(VoicePreferences).where(VoicePreferences.owner_id == actor.user_id))
    return Preferences.model_validate(app.vault.open(row.payload, actor.user_id + ':voice-preferences')) if row else Preferences()


def configured(app, db, actor):
    scope(db, actor.user_id, actor.household_id)
    result = {}
    for name, capability in [('transcription', 'speech.transcribe@v1'), ('synthesis', 'speech.synthesize@v1')]:
        try:
            manifest = app.registry.resolve(db, capability, cloud=False)
            result[name] = {'configured': True, 'provider_id': manifest.id}
        except HTTPException:
            result[name] = {'configured': False, 'provider_id': None}
    return result


def require_speech(app, db, actor):
    if not all(item['configured'] for item in configured(app, db, actor).values()):
        raise HTTPException(503, '请先为当前成员启用本地语音识别和语音合成 Provider')


def call_view(app, call):
    content = app.vault.open(call.payload, call.owner_id + ':voice-call:' + call.id)
    return {'id': call.id, 'status': call.status, 'direction': call.direction,
        'created_at': call.created_at, 'expires_at': call.expires_at, 'reason': content['reason'],
        'source_task_id': call.source_task_id, 'push_status': call.push_status, 'greeting_turn_id': content.get('greeting_turn_id')}


def create_call(app, db, actor, key, *, incoming=False, reason='与你进行语音对话', source_task_id=None, test=False):
    scope(db, actor.user_id, actor.household_id)
    # 按成员锁住检查与创建，避免两个模型步骤同时占用或超过限额。
    db.scalar(select(Principal).where(Principal.id == actor.user_id).with_for_update())
    old = db.scalar(select(VoiceCall).where(VoiceCall.owner_id == actor.user_id, VoiceCall.request_key == key))
    if old: return old
    require_speech(app, db, actor)
    settings = preferences(app, db, actor)
    active = db.scalar(select(VoiceCall).where(VoiceCall.owner_id == actor.user_id, VoiceCall.status.in_(['RINGING', 'ACTIVE']), VoiceCall.expires_at > now()))
    if active: raise HTTPException(409, '已有通话正在进行')
    target = actor.device_id
    if incoming:
        if not test:
            if not settings.incoming_enabled: raise HTTPException(403, '当前成员尚未开启 AI 主动来电')
            hour = datetime.fromtimestamp(now(), ZoneInfo(settings.timezone)).hour
            quiet = (settings.quiet_start <= hour < settings.quiet_end if settings.quiet_start < settings.quiet_end else
                hour >= settings.quiet_start or hour < settings.quiet_end) if settings.quiet_start != settings.quiet_end else False
            if quiet: raise HTTPException(409, '当前在该成员的免打扰时段')
            calls = list(db.scalars(select(VoiceCall).where(VoiceCall.owner_id == actor.user_id, VoiceCall.direction == 'incoming', VoiceCall.created_at > now()-86400)))
            if len(calls) >= settings.daily_limit or any(c.created_at > now()-1800 for c in calls):
                raise HTTPException(429, '已达到主动来电频率限制，请使用普通通知')
        registration = db.scalar(select(VoIPRegistration).join(Device, Device.id == VoIPRegistration.device_id).where(
            VoIPRegistration.owner_id == actor.user_id, Device.user_id == actor.user_id, Device.revoked.is_(False)).order_by(VoIPRegistration.updated_at.desc()).limit(1))
        if not registration: raise HTTPException(409, '没有可接收来电的已配对设备，请在 iOS 语音通话设置注册')
        from .notifications_config import resolve_configuration
        if resolve_configuration(app).status != 'ready': raise HTTPException(503, '服务器 APNs 尚未配置，无法发起来电')
        target = registration.device_id
    identifier = uid()
    call = VoiceCall(id=identifier, owner_id=actor.user_id, household_id=actor.household_id,
        request_key=key, target_device_id=target, source_task_id=source_task_id,
        direction='incoming' if incoming else 'outgoing', status='RINGING' if incoming else 'ACTIVE',
        push_status='PENDING' if incoming else 'NONE', expires_at=now()+(90 if incoming else 1800),
        payload=app.vault.seal({'reason': reason, 'speaker': settings.speaker, 'timezone': settings.timezone}, actor.user_id + ':voice-call:' + identifier))
    db.add(call); db.flush()
    audit(db, actor, 'voice.call.created', call.id, {'direction': call.direction})
    emit(db, actor, 'voice.call.updated', call.id)
    return call


def checked_call(app, db, actor, identifier, *, active=False):
    call = own(db, VoiceCall, identifier, actor)
    db.refresh(call, with_for_update=True)
    if call.target_device_id != actor.device_id: raise HTTPException(403, '通话属于另一台设备')
    if call.source_task_id:
        from .result_access import check_dependencies
        source = own(db, Task, call.source_task_id, actor)
        check_dependencies(db, actor, app.vault.open(source.request, actor.user_id + ':task:' + source.id))
    if call.expires_at <= now() and call.status not in TERMINAL:
        call.status = 'MISSED' if call.status == 'RINGING' else 'ENDED'
    if active and call.status != 'ACTIVE': raise HTTPException(409, '通话尚未接听或已经结束')
    return call


@router.get('/settings')
def read_settings(request: Request, actor=Depends(authenticate)):
    app = request.app.state
    with app.db() as db:
        scope(db, actor.user_id, actor.household_id)
        from .notifications_config import resolve_configuration
        return {'preferences': preferences(app, db, actor).model_dump(), 'capabilities': configured(app, db, actor),
            'apns_ready': resolve_configuration(app).status == 'ready',
            'device_registered': db.scalar(select(VoIPRegistration.id).where(VoIPRegistration.device_id == actor.device_id)) is not None}


@router.put('/settings')
def save_settings(body: Preferences, request: Request, actor=Depends(authenticate)):
    app = request.app.state
    with app.db() as db:
        scope(db, actor.user_id, actor.household_id)
        db.scalar(select(Principal).where(Principal.id == actor.user_id).with_for_update())
        row = db.scalar(select(VoicePreferences).where(VoicePreferences.owner_id == actor.user_id))
        if not row:
            row = VoicePreferences(owner_id=actor.user_id, household_id=actor.household_id, payload=''); db.add(row)
        row.payload = app.vault.seal(body.model_dump(), actor.user_id + ':voice-preferences')
        if not body.incoming_enabled:
            for call in db.scalars(select(VoiceCall).where(VoiceCall.owner_id == actor.user_id, VoiceCall.status == 'RINGING')):
                call.status = 'ENDED'; emit(db, actor, 'voice.call.updated', call.id)
        db.commit(); return body


@router.put('/device')
def register(body: Registration, request: Request, actor=Depends(authenticate)):
    app = request.app.state
    if len(body.token) % 2: raise HTTPException(422, 'VoIP 设备令牌格式无效')
    with app.db() as db:
        scope(db, actor.user_id, actor.household_id)
        db.scalar(select(Device).where(Device.id == actor.device_id).with_for_update())
        row = db.scalar(select(VoIPRegistration).where(VoIPRegistration.device_id == actor.device_id))
        if not row:
            row = VoIPRegistration(owner_id=actor.user_id, household_id=actor.household_id, device_id=actor.device_id, token='', environment=body.environment); db.add(row)
        row.token = app.vault.seal(body.token.lower(), actor.user_id + ':voip:' + actor.device_id)
        row.environment, row.updated_at = body.environment, now()
        db.commit(); return {'registered': True}


@router.delete('/device')
def unregister(request: Request, actor=Depends(authenticate)):
    with request.app.state.db() as db:
        scope(db, actor.user_id, actor.household_id)
        row = db.scalar(select(VoIPRegistration).where(VoIPRegistration.device_id == actor.device_id))
        if row: db.delete(row)
        db.commit(); return {'registered': False}


@router.post('/calls', status_code=201)
def start(body: Start, request: Request, actor=Depends(authenticate)):
    app = request.app.state
    with app.db() as db:
        call = create_call(app, db, actor, 'outgoing:' + body.client_key)
        db.commit(); return call_view(app, call)


@router.post('/test-call', status_code=201)
def test_call(body: Start, request: Request, actor=Depends(authenticate)):
    app = request.app.state
    with app.db() as db:
        call = create_call(app, db, actor, 'test:' + body.client_key, incoming=True, test=True, reason='这是你主动请求的 Home AI 来电测试。')
        db.commit(); return call_view(app, call)


@router.get('/calls/{identifier}')
def read_call(identifier: str, request: Request, actor=Depends(authenticate)):
    app = request.app.state
    with app.db() as db:
        call = checked_call(app, db, actor, identifier)
        call.last_seen = now()
        db.commit(); return call_view(app, call)


@router.post('/calls/{identifier}/accept')
def accept(identifier: str, request: Request, actor=Depends(authenticate)):
    app = request.app.state
    with app.db() as db:
        call = checked_call(app, db, actor, identifier)
        if call.status not in {'RINGING', 'ACTIVE'}: raise HTTPException(409, '来电已结束')
        call.status, call.last_seen, call.expires_at = 'ACTIVE', now(), min(call.created_at+1800, now()+1800)
        content = app.vault.open(call.payload, actor.user_id + ':voice-call:' + call.id)
        if call.direction == 'incoming' and not content.get('greeting_turn_id'):
            from .runtime import submit
            identifier = uid()
            tasks = []
            greeting = '你好，我是 Home AI。' + content['reason']
            for index in range(0, len(greeting), 250):
                task = submit(db, actor, TaskRequest(idempotency_key=f'call-greeting:{call.id}:{index}', capability='speech.synthesize@v1',
                    arguments={'text':greeting[index:index+250], 'speaker':content['speaker']}, timeout_seconds=600, step_timeout_seconds=300), app.vault)
                tasks.append(task.id)
            turn = VoiceTurn(id=identifier, owner_id=actor.user_id, household_id=actor.household_id, call_id=call.id,
                client_key='greeting', request_hash=digest(greeting.encode()), status='SYNTHESIZING',
                payload=app.vault.seal({'tts_tasks':tasks,'answer':greeting}, actor.user_id + ':voice-turn:' + identifier))
            db.add(turn); content['greeting_turn_id'] = identifier
            call.payload = app.vault.seal(content, actor.user_id + ':voice-call:' + call.id)
        db.commit(); return call_view(app, call)


@router.post('/calls/{identifier}/end')
def end(identifier: str, request: Request, actor=Depends(authenticate)):
    app = request.app.state
    with app.db() as db:
        call = checked_call(app, db, actor, identifier)
        if call.status not in TERMINAL: call.status = 'DECLINED' if call.status == 'RINGING' else 'ENDED'
        emit(db, actor, 'voice.call.updated', call.id)
        db.commit(); return call_view(app, call)


def validate_wav(encoded):
    try:
        data = base64.b64decode(encoded, validate=True)
        with wave.open(io.BytesIO(data), 'rb') as stream:
            if stream.getframerate()!=16000 or stream.getnchannels()!=1 or stream.getsampwidth()!=2 or stream.getcomptype()!='NONE': raise ValueError()
            frames = stream.getnframes()
            if not 1600 <= frames <= 960000 or len(stream.readframes(frames)) != frames*2: raise ValueError()
        return data
    except Exception: raise HTTPException(422, '请提交 0.1–60 秒的 16kHz 单声道 PCM WAV') from None


@router.post('/calls/{identifier}/turns', status_code=202)
def audio_turn(identifier: str, body: AudioInput, request: Request, actor=Depends(authenticate)):
    app = request.app.state
    raw = validate_wav(body.content_base64)
    fingerprint = digest(raw)
    with app.db() as db:
        call = checked_call(app, db, actor, identifier, active=True)
        old = db.scalar(select(VoiceTurn).where(VoiceTurn.call_id == call.id, VoiceTurn.client_key == body.client_key))
        if old:
            if old.request_hash != fingerprint: raise HTTPException(409, '相同发送标识不能用于不同音频')
            return {'id': old.id, 'status': old.status}
        if db.scalar(select(VoiceTurn.id).where(VoiceTurn.call_id == call.id, VoiceTurn.status.not_in(['READY', 'FAILED', 'WAITING']))):
            raise HTTPException(409, '上一段语音仍在处理')
        from .runtime import submit
        turn_id = uid()
        task = submit(db, actor, TaskRequest(idempotency_key='call-asr:' + turn_id, capability='speech.transcribe@v1',
            arguments={'content_base64': body.content_base64}, timeout_seconds=300, step_timeout_seconds=180), app.vault)
        turn = VoiceTurn(id=turn_id, owner_id=actor.user_id, household_id=actor.household_id,
            call_id=call.id, client_key=body.client_key, request_hash=fingerprint,
            payload=app.vault.seal({'asr_task': task.id}, actor.user_id + ':voice-turn:' + turn_id))
        db.add(turn); call.last_seen = now(); db.commit()
        return {'id': turn.id, 'status': turn.status}


@router.get('/calls/{identifier}/turns/{turn_id}')
def read_turn(identifier: str, turn_id: str, request: Request, actor=Depends(authenticate)):
    app = request.app.state
    with app.db() as db:
        call = checked_call(app, db, actor, identifier, active=True)
        turn = own(db, VoiceTurn, turn_id, actor)
        if turn.call_id != call.id: raise HTTPException(404, '语音片段不存在')
        payload = app.vault.open(turn.payload, actor.user_id + ':voice-turn:' + turn.id)
        from .result_access import check_dependencies
        if payload.get('chat_task'):
            task = own(db, Task, payload['chat_task'], actor)
            check_dependencies(db, actor, app.vault.open(task.request, actor.user_id + ':task:' + task.id))
        call.last_seen = now(); db.commit()
        return {'id': turn.id, 'status': turn.status, 'transcript': payload.get('transcript', ''),
            'answer': payload.get('answer', ''), 'audio_count': len(payload.get('tts_tasks', [])) if turn.status in {'READY','WAITING'} else 0,
            'task_id': payload.get('chat_task'), 'error': payload.get('error')}


@router.get('/calls/{identifier}/turns/{turn_id}/audio/{index}')
def read_audio(identifier: str, turn_id: str, index: int, request: Request, actor=Depends(authenticate)):
    state = read_turn(identifier, turn_id, request, actor)
    if state['status'] not in {'READY','WAITING'} or not 0 <= index < state['audio_count']: raise HTTPException(404, '语音尚未生成')
    app = request.app.state
    with app.db() as db:
        checked_call(app, db, actor, identifier, active=True)
        turn = own(db, VoiceTurn, turn_id, actor)
        payload = app.vault.open(turn.payload, actor.user_id + ':voice-turn:' + turn.id)
        if payload.get('chat_task'):
            from .result_access import check_dependencies
            source = own(db, Task, payload['chat_task'], actor)
            check_dependencies(db, actor, app.vault.open(source.request, actor.user_id + ':task:' + source.id))
        task = own(db, Task, payload['tts_tasks'][index], actor)
        if task.status != 'SUCCEEDED' or not task.result: raise HTTPException(409, '语音合成尚未成功')
        return app.vault.open(task.result, actor.user_id + ':task-result:' + task.id)


TOOL = {'type': 'function', 'function': {'name': 'call_user',
    'description': '确实需要实时语音沟通时呼叫当前用户的Home AI。仅本人已开启主动来电且不在免打扰时间时可用，有频率限制；不是拨打电话网号码，不调用它收集密码或替代授权。不能仅为普通结果反复打电话。成功只表示已创建来电请求，不代表用户接听。',
    'parameters': {'type': 'object', 'properties': {'reason': {'type': 'string', 'maxLength': 500}}, 'required': ['reason'], 'additionalProperties': False}}}
