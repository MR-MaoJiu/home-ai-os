import asyncio
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
import nats
from nats.js.errors import NotFoundError
from croniter import croniter
from sqlalchemy import select, delete, or_
from .api import create_app
from .db import Principal, Task, Outbox, Nonce, Automation, Device, Invocation, Approval, now, scope
from .runtime import run_task, submit
from .contracts import TaskRequest
from .data import emit, audit
from .security import Actor

log = logging.getLogger("homeai.worker")


async def cycle(app, js=None):
    with app.db() as db:
        users = [(u.id, u.household_id, u.role) for u in db.scalars(select(Principal))]
    for user_id, household, role in users:
        with app.db() as db:
            scope(db, user_id, household)
            expired_approvals = select(Invocation.task_id).join(Approval, Approval.invocation_id == Invocation.id).where(Approval.expires_at <= now())
            for task in db.scalars(select(Task).where(Task.owner_id == user_id, Task.status == "AWAITING_APPROVAL", or_(Task.deadline <= now(), Task.id.in_(expired_approvals))).with_for_update(skip_locked=True)):
                task.status, task.error = "FAILED", "任务或审批已过期，未执行后续操作"
                actor = Actor(user_id, household, "scheduler", role)
                emit(db, actor, "task.updated", task.id)
                audit(db, actor, "task.expired", task.id)
            db.commit()
            pending = list(db.scalars(select(Task.id).where(Task.owner_id == user_id, Task.status.in_(["RECEIVED", "APPROVED", "EXECUTING"])).limit(20)))
        for task_id in pending:
            await run_task(app, task_id, user_id)
        with app.db() as db:
            scope(db, user_id, household)
            # 自动化逐步骤按固定幂等键提交；每步经过同一审批与策略路径。
            for automation in db.scalars(select(Automation).where(Automation.owner_id == user_id, Automation.enabled.is_(True), Automation.trigger_kind == "cron", Automation.next_run <= now()).with_for_update(skip_locked=True)):
                skill = app.vault.open(automation.skill, user_id + ":automation:" + automation.id)
                actor = Actor(user_id, household, skill["device_id"], role)
                device = db.get(Device, actor.device_id)
                if not device or device.revoked:
                    automation.enabled = False
                    continue
                submit(db, actor, TaskRequest(idempotency_key=f"auto:{automation.id}:{automation.next_run}", steps=skill["steps"], max_steps=len(skill["steps"]), timezone=automation.timezone), app.vault)
                automation.next_run = croniter(automation.cron, datetime.now(ZoneInfo(automation.timezone))).get_next(float)
            db.commit()
        from .event_automations import dispatch
        dispatch(app, user_id, household, role)
        if js:
            with app.db() as db:
                scope(db, user_id, household)
                events = db.scalars(select(Outbox).where(Outbox.owner_id == user_id, Outbox.published.is_(False)).order_by(Outbox.id).limit(100)).all()
                for event in events:
                    await js.publish(app.settings.event_subject_prefix + "." + event.kind, json.dumps({"schema_version": "1.0", "event_id": event.event_id, "owner_id": event.owner_id, "household_id": event.household_id, "type": event.kind, "resource_id": event.resource_id}).encode(), headers={"Nats-Msg-Id": event.event_id})
                    event.published = True
                db.commit()


async def main():
    app = create_app().state
    nc = await nats.connect(app.settings.nats_url)
    js = nc.jetstream()
    try:
        await js.stream_info(app.settings.event_stream)
    except NotFoundError:
        await js.add_stream(name=app.settings.event_stream, subjects=[app.settings.event_subject_prefix + ".>"])
    from .event_automations import subscription, drain
    sub = await subscription(app, js)
    try:
        while True:
            try:
                await cycle(app, js)
                await drain(app, sub)
                with app.db() as db:
                    db.execute(delete(Nonce).where(Nonce.expires_at < now()))
                    db.commit()
            except Exception as exc:
                log.error("工作循环失败：%s", type(exc).__name__)
            await asyncio.sleep(1)
    finally:
        await nc.drain()


if __name__ == "__main__":
    asyncio.run(main())
