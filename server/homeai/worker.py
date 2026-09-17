import asyncio
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
import nats
from nats.js.errors import NotFoundError
from croniter import croniter
from sqlalchemy import select, delete
from .api import create_app
from .db import Principal, Task, Outbox, Nonce, Automation, Device, Invocation, now, scope
from .runtime import run_task, submit
from .contracts import TaskRequest
from .security import Actor

log = logging.getLogger("homeai.worker")


async def cycle(app, js=None):
    with app.db() as db:
        users = [(u.id, u.household_id, u.role) for u in db.scalars(select(Principal))]
    for user_id, household, role in users:
        with app.db() as db:
            scope(db, user_id, household)
            pending = list(db.scalars(select(Task.id).where(Task.owner_id == user_id, Task.status.in_(["RECEIVED", "APPROVED"])).limit(20)))
        for task_id in pending:
            await run_task(app, task_id, user_id)
        with app.db() as db:
            scope(db, user_id, household)
            # 自动化逐步骤按固定幂等键提交；每步经过同一审批与策略路径。
            for automation in db.scalars(select(Automation).where(Automation.owner_id == user_id, Automation.enabled.is_(True), Automation.next_run <= now()).with_for_update(skip_locked=True)):
                skill = app.vault.open(automation.skill, user_id + ":automation:" + automation.id)
                actor = Actor(user_id, household, skill["device_id"], role)
                device = db.get(Device, actor.device_id)
                if not device or device.revoked:
                    automation.enabled = False
                    continue
                # 仅支持无步骤依赖的单动作，复杂工作流不能伪装为并行任务。
                if len(skill["steps"]) != 1:
                    automation.enabled = False
                    continue
                step = skill["steps"][0]
                submit(db, actor, TaskRequest(idempotency_key=f"auto:{automation.id}:{automation.next_run}", capability=step["capability"], arguments=step["arguments"]), app.vault)
                automation.next_run = croniter(automation.cron, datetime.now(ZoneInfo(automation.timezone))).get_next(float)
            db.commit()
        if js:
            with app.db() as db:
                scope(db, user_id, household)
                events = db.scalars(select(Outbox).where(Outbox.owner_id == user_id, Outbox.published.is_(False)).order_by(Outbox.id).limit(100)).all()
                for event in events:
                    await js.publish("homeai.events." + event.kind, json.dumps({"schema_version": "1.0", "event_id": event.event_id, "owner_id": event.owner_id, "household_id": event.household_id, "type": event.kind, "resource_id": event.resource_id}).encode(), headers={"Nats-Msg-Id": event.event_id})
                    event.published = True
                db.commit()


async def main():
    app = create_app().state
    # 重启时把执行中任务标记为待核对，不自动重放未知副作用。
    with app.db() as db:
        users = [(u.id, u.household_id) for u in db.scalars(select(Principal))]
    for user_id, household in users:
        with app.db() as db:
            scope(db, user_id, household)
            for t in db.scalars(select(Task).where(Task.owner_id == user_id, Task.status == "EXECUTING")):
                t.status, t.error = "NEEDS_RECONCILIATION", "执行进程中断，请核对外部操作结果"
            db.commit()
    nc = await nats.connect(app.settings.nats_url)
    js = nc.jetstream()
    try:
        await js.stream_info("HOMEAI")
    except NotFoundError:
        await js.add_stream(name="HOMEAI", subjects=["homeai.events.>"])
    try:
        while True:
            try:
                await cycle(app, js)
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
