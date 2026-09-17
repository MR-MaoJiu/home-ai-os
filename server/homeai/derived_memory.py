"""可重建的外部记忆投影。每次变更先清除主体派生数据，再读取规范账本。"""
import hashlib
import json
from fastapi import HTTPException
from sqlalchemy import select, func, text
from .contracts import ProviderManifest
from .db import Provider, Record, Outbox, DerivedJob, scope
from .security import Actor
from .data import serialize


def checkpoint(db, user_id, manifest):
    sequence = db.scalar(select(func.max(Outbox.id)).where(Outbox.owner_id == user_id, Outbox.kind.in_(['record.changed', 'record.deleted']))) or 0
    return hashlib.sha256((manifest.model_dump_json() + ':' + str(sequence)).encode()).hexdigest()


def job_id(user_id, provider_id):
    return hashlib.sha256((user_id + ':' + provider_id).encode()).hexdigest()


def require_ready(db, actor, manifest):
    job = db.get(DerivedJob, job_id(actor.user_id, manifest.id))
    if not job or job.status != 'READY' or job.event_id != checkpoint(db, actor.user_id, manifest):
        raise HTTPException(503, '派生记忆正在重建或尚未同步，请使用核心记忆检索')


async def reconcile_provider(app, user_id, household_id, provider_id):
    with app.db() as db:
        scope(db, user_id, household_id)
        if db.bind.dialect.name == 'postgresql':
            if not db.scalar(text('SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))'), {'key': 'derived:' + user_id + ':' + provider_id}):
                return
        provider = db.get(Provider, provider_id)
        if not provider or not provider.enabled:
            return
        manifest = ProviderManifest.model_validate_json(provider.manifest)
        if manifest.cloud:
            return
        prefix = next((p for p in ('memory.semantic', 'memory.graph') if p + '.index@v1' in manifest.capabilities and p + '.purge@v1' in manifest.capabilities), None)
        if not prefix:
            return
        target = checkpoint(db, user_id, manifest)
        identity = job_id(user_id, provider_id)
        job = db.get(DerivedJob, identity)
        if job and job.status == 'READY' and job.event_id == target:
            return
        if not job:
            job = DerivedJob(id=identity, owner_id=user_id, household_id=household_id, provider_id=provider_id, event_id=target, attempts=0)
            db.add(job)
        actor = Actor(user_id, household_id, 'memory-indexer', 'service')
        job.attempts += 1
        job.status, job.event_id = 'REBUILDING', target
        try:
            await app.policy.check(actor, prefix + '.purge@v1')
            await app.policy.check(actor, prefix + '.index@v1')
            await app.registry.invoke(db, actor, manifest, prefix + '.purge@v1', {}, identity + ':purge:' + target)
            records = db.scalars(select(Record).where(Record.owner_id == user_id, Record.deleted.is_(False), Record.kind == 'memory.fact', Record.sensitivity != 'SECRET').order_by(Record.id)).all()
            for record in records:
                db.refresh(record)
                if record.deleted or record.sensitivity == 'SECRET' or record.kind != 'memory.fact':
                    continue
                arguments = {'record_id': record.id, 'content': json.dumps(serialize(record, app.vault)['payload'], ensure_ascii=False)}
                await app.registry.invoke(db, actor, manifest, prefix + '.index@v1', arguments, identity + ':' + record.id + ':' + str(record.version))
            # 重建期间发生删除、修改或停用，不允许发布这份旧投影。
            db.refresh(provider)
            job.status = 'READY' if provider.enabled and ProviderManifest.model_validate_json(provider.manifest) == manifest and checkpoint(db, user_id, manifest) == target else 'PENDING'
            job.error = None
        except Exception as exc:
            # 部分写入会在下一次重试前整体清除，不能把不明结果当作已完成。
            job.status, job.error = 'FAILED', type(exc).__name__
        db.commit()


async def reconcile(app, user_id, household_id):
    with app.db() as db:
        provider_ids = list(db.scalars(select(Provider.id).where(Provider.enabled.is_(True))))
    for provider_id in provider_ids:
        await reconcile_provider(app, user_id, household_id, provider_id)
