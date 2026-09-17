"""只根据规范 Outbox 生成投递；消息中的主体和事件内容不能授予权限。"""
import hashlib
import json
from fastapi import HTTPException
from sqlalchemy import select, text
from nats.js.api import ConsumerConfig, AckPolicy
from .db import Principal, Outbox, Automation, AutomationDelivery, Consumption, Device, now, scope
from .contracts import TaskRequest
from .data import read_record, audit
from .runtime import submit
from .security import Actor

CONSUMER = 'automation-v1'


def consume(app, payload, subject):
    """事务持久化成功才返回；调用方随后 ACK，崩溃重投不重复排队。"""
    if not isinstance(payload, dict):
        return
    event_id, owner_id = payload.get('event_id'), payload.get('owner_id')
    if not isinstance(event_id, str) or not isinstance(owner_id, str) or max(len(event_id), len(owner_id)) > 100:
        return
    with app.db() as db:
        principal = db.get(Principal, owner_id)
        if not principal:
            return
        scope(db, principal.id, principal.household_id)
        key = CONSUMER + ':' + event_id
        if db.get_bind().dialect.name == 'postgresql':
            lock = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], 'big', signed=True)
            db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock})
        event = db.scalar(select(Outbox).where(Outbox.event_id == event_id, Outbox.owner_id == owner_id))
        if not event or subject != app.settings.event_subject_prefix + '.' + event.kind:
            return
        if db.get(Consumption, key):
            return
        db.add(Consumption(id=key))
        rules = db.scalars(select(Automation).where(
            Automation.owner_id == owner_id, Automation.enabled.is_(True),
            Automation.trigger_kind == 'event', Automation.event_type == event.kind,
            Automation.created_at <= event.created_at).with_for_update()).all()
        chain = json.loads(event.automation_chain)
        for rule in rules:
            if not event.record_owner_id or (not rule.include_shared and event.record_owner_id != owner_id):
                continue
            if rule.record_kind and rule.record_kind != event.record_kind:
                continue
            if rule.record_source and rule.record_source != event.record_source:
                continue
            reason = '已阻止自动化循环或超过四层因果链' if rule.id in chain or len(chain) >= 4 else None
            db.add(AutomationDelivery(owner_id=owner_id, household_id=principal.household_id,
                automation_id=rule.id, event_id=event_id,
                status='SKIPPED' if reason else 'PENDING', reason=reason))
        db.commit()


def event_arguments(value, event):
    """仅替换整个值的固定引用，既不执行代码，也不把正文隐式送入工作流。"""
    if isinstance(value, dict):
        if '$event' in value:
            fields = {'record_id': event.resource_id, 'event_id': event.event_id, 'event_type': event.kind}
            if set(value) != {'$event'} or not isinstance(value['$event'], str) or value['$event'] not in fields:
                raise ValueError('事件引用只能是 record_id、event_id 或 event_type')
            return fields[value['$event']]
        return {key: event_arguments(item, event) for key, item in value.items()}
    if isinstance(value, list):
        return [event_arguments(item, event) for item in value]
    return value


def dispatch(app, user_id, household, role):
    with app.db() as db:
        scope(db, user_id, household)
        # 与停用接口、其他调度进程保持相同的规则→投递锁顺序。
        rules = db.scalars(select(Automation).where(Automation.owner_id == user_id,
            Automation.trigger_kind == 'event').with_for_update(skip_locked=True)).all()
        for rule in rules:
            deliveries = db.scalars(select(AutomationDelivery).where(
                AutomationDelivery.automation_id == rule.id, AutomationDelivery.status == 'PENDING')
                .order_by(AutomationDelivery.created_at, AutomationDelivery.id).limit(100).with_for_update()).all()
            for delivery in deliveries:
                if not rule.enabled:
                    delivery.status, delivery.reason = 'CANCELED', '自动化已停用'
                    continue
                if rule.last_trigger_at + rule.cooldown_seconds > now():
                    break
                event = db.scalar(select(Outbox).where(Outbox.event_id == delivery.event_id))
                skill = app.vault.open(rule.skill, user_id + ':automation:' + rule.id)
                actor = Actor(user_id, household, skill['device_id'], role)
                device = db.get(Device, actor.device_id)
                if not device or device.revoked or device.user_id != user_id:
                    rule.enabled = False
                    delivery.status, delivery.reason = 'CANCELED', '执行设备已撤销'
                    continue
                try:
                    if not event:
                        raise ValueError('规范事件不存在')
                    dependencies = None
                    if event.kind == 'record.changed':
                        record = read_record(db, actor, event.resource_id)
                        if record.version != event.record_version or record.sensitivity == 'SECRET':
                            raise ValueError('事件来源版本已变化或为秘密数据')
                        dependencies = {record.id: record.version}
                    steps = event_arguments(skill['steps'], event)
                    task = submit(db, actor, TaskRequest(idempotency_key=f'event:{rule.id}:{event.event_id}',
                        steps=steps, max_steps=len(steps)), app.vault,
                        automation_chain=json.loads(event.automation_chain) + [rule.id],
                        record_dependencies=dependencies)
                    delivery.task_id, delivery.status = task.id, 'DISPATCHED'
                    rule.last_trigger_at = now()
                    audit(db, actor, 'automation.dispatch', rule.id, {'event_id': event.event_id, 'task_id': task.id})
                except (HTTPException, ValueError):
                    delivery.status, delivery.reason = 'SKIPPED', '来源不可访问、版本变化或工作流参数无效'
        db.commit()


async def subscription(app, js):
    return await js.pull_subscribe(app.settings.event_subject_prefix + '.record.*', durable=CONSUMER,
        stream=app.settings.event_stream, config=ConsumerConfig(ack_policy=AckPolicy.EXPLICIT,
        ack_wait=60, max_ack_pending=100))


async def drain(app, sub):
    try:
        messages = await sub.fetch(batch=50, timeout=0.5)
    except TimeoutError:
        return
    for message in messages:
        try:
            payload = json.loads(message.data)
        except (ValueError, UnicodeError):
            await message.term()
            continue
        try:
            consume(app, payload, message.subject)
        except Exception:
            await message.nak(delay=5)
            raise
        await message.ack_sync(timeout=2)
