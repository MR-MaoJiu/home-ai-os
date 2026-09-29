"""服务端通知箱与 APNs 投递。推送仅含通知标识，正文始终在家庭服务器鉴权读取。"""
import asyncio
import base64
import json
import logging
import re
from datetime import datetime
from typing import Literal

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field, field_validator
from sqlalchemy import exists, select, text

from .contracts import Contract
from .crypto import digest
from .data import audit
from .db import (Automation, Consumption, ConversationTurn, Device, Notification, Outbox,
                 Principal, PushDelivery, PushRegistration, Record, Task, now, scope, uid)
from .security import Actor, authenticate, own
from .notifications_config import environment_configuration, resolve_configuration, parse_key

router = APIRouter(prefix='/api/v1', tags=['通知'])
log = logging.getLogger('homeai.notifications')
NOTIFY_STATUSES = {'SUCCEEDED', 'FAILED', 'CANCELED', 'AWAITING_APPROVAL', 'NEEDS_RECONCILIATION'}


class PushInput(Contract):
    token: str = Field(min_length=32, max_length=512)
    environment: Literal['sandbox', 'production']
    authorization: Literal['authorized', 'provisional']

    @field_validator('token')
    @classmethod
    def valid_token(cls, value):
        if len(value) % 2 or not re.fullmatch('[0-9a-fA-F]+', value):
            raise ValueError('APNs 设备标识必须是十六进制字符串')
        return value.lower()


def push_configuration(settings):
    return environment_configuration(settings).status


def worker_online(app):
    from .heartbeat import configuration_id
    for path in (app.settings.state_dir / 'health').glob('notification-worker-*.enc'):
        try:
            if path.is_symlink() or path.stat().st_size > 10000:
                continue
            item = app.vault.open(path.read_text(), 'worker-heartbeat:notification-worker')
            if item['configuration'] == configuration_id(app) and item['phase'] == 'running' and 0 <= now() - item['last_seen'] <= 30:
                return True
        except Exception:
            continue
    return False


@router.get('/notifications/status')
def notification_status(request: Request, actor: Actor = Depends(authenticate)):
    app = request.app.state
    status = resolve_configuration(app).status
    online = worker_online(app)
    return {'push_configured': status == 'ready', 'worker_online': online,
            'status': status if status != 'ready' or online else 'worker_offline'}


@router.put('/devices/current/push')
def register_push(body: PushInput, request: Request, actor: Actor = Depends(authenticate)):
    app = request.app.state
    with app.db() as db:
        scope(db, actor.user_id, actor.household_id)
        device = db.get(Device, actor.device_id)
        if not device or device.revoked or device.user_id != actor.user_id:
            raise HTTPException(403, '只能为当前已配对设备注册推送')
        row = db.scalar(select(PushRegistration).where(PushRegistration.device_id == actor.device_id).with_for_update())
        if not row:
            row = PushRegistration(id=uid(), owner_id=actor.user_id, household_id=actor.household_id, device_id=actor.device_id)
            db.add(row)
        row.token = app.vault.seal(body.token, actor.user_id + ':push:' + actor.device_id)
        row.token_digest = digest(body.token.encode())
        row.environment, row.authorization, row.enabled, row.updated_at = body.environment, body.authorization, True, now()
        audit(db, actor, 'push.register', actor.device_id)
        db.commit()
    return {'registered': True, **notification_status(request, actor)}


@router.delete('/devices/current/push')
def unregister_push(request: Request, actor: Actor = Depends(authenticate)):
    with request.app.state.db() as db:
        scope(db, actor.user_id, actor.household_id)
        row = db.scalar(select(PushRegistration).where(PushRegistration.device_id == actor.device_id))
        if row:
            row.enabled = False
            row.token = ''
            row.updated_at = now()
        audit(db, actor, 'push.unregister', actor.device_id)
        db.commit()
    return {'registered': False}


def visible(db, notification):
    if notification.visibility == 'personal':
        return True
    if notification.automation_id:
        row = db.get(Automation, notification.automation_id)
    elif notification.record_id:
        row = db.get(Record, notification.record_id)
    else:
        return False
    return bool(row and row.household_id == notification.household_id and not getattr(row, 'deleted', False)
                and (row.owner_id == notification.owner_id or getattr(row, 'visibility', 'personal') == 'family'))


def view(row):
    return {'id': row.id, 'kind': row.kind, 'scope': row.visibility, 'created_at': row.created_at,
            'read_at': row.read_at, 'task_id': row.task_id, 'conversation_id': row.conversation_id,
            'automation_id': row.automation_id, 'record_id': row.record_id, 'status': row.status}


@router.get('/notifications')
def list_notifications(request: Request, actor: Actor = Depends(authenticate),
                       before: float | None = Query(default=None, gt=0), limit: int = Query(default=50, ge=1, le=100)):
    with request.app.state.db() as db:
        scope(db, actor.user_id, actor.household_id)
        query = select(Notification).where(Notification.owner_id == actor.user_id, Notification.household_id == actor.household_id)
        if before is not None:
            query = query.where(Notification.created_at < before)
        rows = list(db.scalars(query.order_by(Notification.created_at.desc(), Notification.id).limit(limit + 1)))
        more = len(rows) > limit
        rows = rows[:limit]
        return {'items': [view(row) for row in rows if visible(db, row)], 'has_more': more,
                'next_before': rows[-1].created_at if more and rows else None}


@router.get('/notifications/{notification_id}')
def get_notification(notification_id: str, request: Request, actor: Actor = Depends(authenticate)):
    with request.app.state.db() as db:
        row = own(db, Notification, notification_id, actor)
        if not visible(db, row):
            raise HTTPException(404, '通知不存在或已撤回')
        return view(row)


@router.post('/notifications/{notification_id}/read')
def read_notification(notification_id: str, request: Request, actor: Actor = Depends(authenticate)):
    with request.app.state.db() as db:
        row = own(db, Notification, notification_id, actor)
        if not visible(db, row):
            raise HTTPException(404, '通知不存在或已撤回')
        row.read_at = row.read_at or now()
        db.commit()
        return {'id': row.id, 'read_at': row.read_at}


def create_notification(db, household, recipient, event_key, kind, visibility, status, occurred_at=None, **references):
    scope(db, recipient, household)
    if db.scalar(select(Notification.id).where(Notification.owner_id == recipient, Notification.event_key == event_key)):
        return
    row = Notification(id=uid(), household_id=household, owner_id=recipient, event_key=event_key,
                       kind=kind, visibility=visibility, status=status, created_at=occurred_at if occurred_at is not None else now(), **references)
    db.add(row)
    db.flush()
    # 离线后仍可查看到期历史，但不能把很久以前的到期提醒重新包装成即时推送。
    if row.created_at < now() - 86400:
        return
    for registration in db.scalars(select(PushRegistration).where(PushRegistration.owner_id == recipient, PushRegistration.enabled.is_(True))):
        device = db.get(Device, registration.device_id)
        if device and not device.revoked and device.user_id == recipient:
            db.add(PushDelivery(id=uid(), household_id=household, owner_id=recipient,
                               notification_id=row.id, device_id=device.id))


def collect_task_events(app, user_id, household):
    """独立消费 Outbox，不依赖 NATS 是否已发布；收件箱和消费标识原子提交。"""
    with app.db() as db:
        scope(db, user_id, household)
        query = select(Outbox).where(Outbox.owner_id == user_id, Outbox.household_id == household,
            Outbox.kind.in_(['task.updated', 'task.approval_required']), ~exists(select(Consumption.id).where(Consumption.id == 'notification:' + Outbox.event_id)))
        for event in db.scalars(query.order_by(Outbox.id).limit(100)).all():
            scope(db, user_id, household)
            # Outbox 没有保存当时的任务状态。只允许最新事件读取当前状态，避免旧的
            # 中间步骤事件抢占完成通知的幂等键；过期事件仅消费，不生成新通知。
            task = db.scalar(select(Task).where(Task.id == event.resource_id).with_for_update())
            latest_event_id = db.scalar(select(Outbox.id).where(Outbox.owner_id == user_id,
                Outbox.household_id == household, Outbox.resource_id == event.resource_id,
                Outbox.kind.in_(['task.updated', 'task.approval_required'])).order_by(Outbox.id.desc()).limit(1))
            current_event = event.id == latest_event_id and now() - 86400 <= event.created_at <= now() + 60
            if current_event and task and task.owner_id == user_id and task.status in NOTIFY_STATUSES:
                body = app.vault.open(task.request, user_id + ':task:' + task.id)
                automation = db.get(Automation, body.get('_automation_id')) if body.get('_automation_id') else None
                family = bool(automation and automation.owner_id == user_id and automation.household_id == household
                              and getattr(automation, 'visibility', 'personal') == 'family'
                              and body.get('_automation_scope') == 'family')
                # 只有执行者处理审批；其他成员不会收到私人任务或审批标识。
                recipients = list(db.scalars(select(Principal.id).where(Principal.household_id == household))) if family and task.status in {'SUCCEEDED', 'FAILED', 'CANCELED'} else [user_id]
                conversation = db.scalar(select(ConversationTurn.conversation_id).where(ConversationTurn.task_id == task.id))
                event_key = 'task:' + task.id + ':' + task.status
                if task.status == 'AWAITING_APPROVAL':
                    from .db import Invocation, Approval
                    pending = db.scalar(select(Approval).join(Invocation, Invocation.id == Approval.invocation_id).where(Invocation.task_id == task.id, Approval.decision == 'PENDING'))
                    event_key += ':' + (pending.id + ':' + str(pending.expires_at) if pending else 'pending')
                for recipient in recipients:
                    create_notification(db, household, recipient, event_key, 'automation.updated' if automation else 'task.updated',
                        'family' if family else 'personal', task.status, occurred_at=event.created_at,
                        task_id=task.id if recipient == user_id else None,
                        conversation_id=conversation if recipient == user_id else None,
                        automation_id=automation.id if automation else None)
            scope(db, user_id, household)
            db.add(Consumption(id='notification:' + event.event_id))
        db.commit()


def collect_due_reminders(app, user_id, household):
    """到期检查只在服务端执行，重复检查与重启不会重复生成通知。"""
    with app.db() as db:
        scope(db, user_id, household)
        rows = list(db.scalars(select(Record).where(Record.owner_id == user_id, Record.household_id == household,
                    Record.kind == 'reminder.item', Record.deleted.is_(False))))
        for row in rows:
            payload = app.vault.open(row.payload, user_id + ':record:' + row.id)
            if not payload.get('notify_at_due') or payload.get('completed') or not payload.get('due_at'):
                continue
            try:
                due = datetime.fromisoformat(payload['due_at'].replace('Z', '+00:00'))
                if due.tzinfo is None or due.timestamp() > now():
                    continue
            except (ValueError, TypeError, OverflowError):
                continue
            event_key = 'reminder:' + row.id + ':' + str(due.timestamp())
            if db.get(Consumption, event_key):
                continue
            family = getattr(row, 'visibility', 'personal') == 'family'
            recipients = list(db.scalars(select(Principal.id).where(Principal.household_id == household))) if family else [user_id]
            for recipient in recipients:
                create_notification(db, household, recipient, event_key, 'reminder.due',
                                    'family' if family else 'personal', 'DUE', occurred_at=due.timestamp(), record_id=row.id)
            scope(db, user_id, household)
            db.add(Consumption(id=event_key))
        db.commit()


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode()


class APNsConfigurationUnavailable(Exception):
    pass


class APNs:
    def __init__(self, settings, client=None, app=None):
        self.settings, self.app = settings, app
        self.client = client
        self.owns_client = client is None
        self.jwt = None
        self.jwt_at = 0
        self.configuration = None

    async def refresh(self):
        config = resolve_configuration(self.app) if self.app else environment_configuration(self.settings)
        if self.configuration is None or self.configuration.revision != config.revision:
            self.jwt, self.jwt_at = None, 0
            self.configuration = config
            # Apple 将连接绑定到团队/Topic；轮换配置时重建 HTTP/2 连接。
            if self.owns_client and self.client is not None:
                await self.client.aclose()
                self.client = None
        if config.status == 'ready' and self.client is None:
            self.client = httpx.AsyncClient(http2=True, timeout=20, trust_env=False)
        return config.status

    async def close(self):
        if self.owns_client and self.client is not None:
            await self.client.aclose()
            self.client = None

    def authorization(self):
        config = self.configuration or environment_configuration(self.settings)
        if config.status != 'ready':
            raise APNsConfigurationUnavailable()
        if self.jwt and now() - self.jwt_at < 3000:
            return self.jwt
        key = parse_key(config.private_key)
        issued = int(now())
        header = b64(json.dumps({'alg': 'ES256', 'kid': config.key_id}, separators=(',', ':')).encode())
        claims = b64(json.dumps({'iss': config.team_id, 'iat': issued}, separators=(',', ':')).encode())
        message = header + '.' + claims
        r, s = decode_dss_signature(key.sign(message.encode(), ec.ECDSA(hashes.SHA256())))
        self.jwt = message + '.' + b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))
        self.jwt_at = issued
        return self.jwt

    async def send(self, token, environment, delivery_id, notification_id):
        if await self.refresh() != 'ready':
            raise APNsConfigurationUnavailable()
        endpoint = 'https://api.sandbox.push.apple.com' if environment == 'sandbox' else 'https://api.push.apple.com'
        payload = {'aps': {'alert': {'title': 'Home AI', 'body': '家庭服务器有新的通知，请打开查看。'}, 'sound': 'default'},
                   'notification_id': notification_id}
        headers = {'authorization': 'bearer ' + self.authorization(), 'apns-topic': self.configuration.topic,
                   'apns-push-type': 'alert', 'apns-priority': '10', 'apns-id': delivery_id,
                   'apns-collapse-id': notification_id, 'apns-expiration': str(int(now()) + 86400)}
        return await self.client.post(endpoint + '/3/device/' + token, json=payload, headers=headers)


async def deliver(app, apns, user_id, household):
    if await apns.refresh() != 'ready':
        return
    with app.db() as db:
        scope(db, user_id, household)
        rows = list(db.scalars(select(PushDelivery).where(PushDelivery.owner_id == user_id,
            PushDelivery.status.in_(['pending', 'retry']), PushDelivery.retry_at <= now()).order_by(PushDelivery.retry_at).limit(30)))
        for row in rows:
            notification = db.get(Notification, row.notification_id)
            device = db.get(Device, row.device_id)
            registration = db.scalar(select(PushRegistration).where(PushRegistration.device_id == row.device_id))
            if not notification or notification.read_at or not visible(db, notification) or not device or device.revoked or device.user_id != user_id or not registration or not registration.enabled:
                row.status = 'canceled'
                if registration and device and device.revoked:
                    registration.enabled = False
                db.commit()
                continue
            if notification.created_at < now() - 86400:
                row.status, row.last_error = 'expired', '超过推送投递时限；仍可在通知箱查看'
                db.commit()
                continue
            row.attempts += 1
            token_digest = registration.token_digest
            registered_at = registration.updated_at
            try:
                token = app.vault.open(registration.token, user_id + ':push:' + row.device_id)
                response = await apns.send(token, registration.environment, row.id, notification.id)
                if response.status_code == 200:
                    row.status, row.accepted_at, row.last_error = 'accepted', now(), None
                else:
                    try:
                        reason = response.json().get('reason', '')
                    except ValueError:
                        reason = ''
                    # 只保存协议定义的原因，不落上游正文、Token 或任何凭据。
                    reason = reason if re.fullmatch('[A-Za-z]{1,80}', reason) else 'APNsHTTP' + str(response.status_code)
                    row.last_error = reason
                    if response.status_code == 410 or reason in {'BadDeviceToken', 'DeviceTokenNotForTopic'}:
                        row.status = 'failed'
                        db.refresh(registration)
                        if registration.token_digest == token_digest and registration.updated_at <= registered_at:
                            registration.enabled = False
                    elif response.status_code in {429, 500, 503} or reason == 'ExpiredProviderToken':
                        row.status = 'retry'
                        apns.jwt = None
                    else:
                        row.status = 'failed'
            except APNsConfigurationUnavailable:
                row.attempts -= 1
                db.commit()
                return
            except (httpx.HTTPError, OSError, ValueError):
                row.status, row.last_error = 'retry', 'transport_error'
            if row.status == 'retry':
                row.retry_at = now() + min(3600, 30 * 2 ** min(row.attempts, 7))
                if row.attempts >= 8:
                    row.status = 'failed'
            db.commit()


async def cycle(app, apns=None):
    with app.db() as db:
        users = [(p.id, p.household_id) for p in db.scalars(select(Principal))]
    for user_id, household in users:
        collect_task_events(app, user_id, household)
        collect_due_reminders(app, user_id, household)
        if apns:
            await deliver(app, apns, user_id, household)


async def main():
    from .api import create_app
    from .heartbeat import Heartbeat
    app = create_app().state
    with app.db.kw['bind'].connect() as lock:
        if not lock.scalar(text('SELECT pg_try_advisory_lock(804225)')):
            raise SystemExit('已有通知 Worker 运行')
        apns = APNs(app.settings, app=app)
        try:
            async with Heartbeat(app, 'notification-worker') as heartbeat:
                while True:
                    try:
                        await cycle(app, apns)
                        heartbeat.progress()
                    except Exception as exc:
                        heartbeat.progress(exc)
                        log.error('通知循环失败：%s', type(exc).__name__)
                    await asyncio.sleep(5)
        finally:
            await apns.close()
