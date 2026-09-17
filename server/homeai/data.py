import json
from fastapi import HTTPException
from sqlalchemy import select, or_, and_
from .db import Record, Revision, Grant, Audit, Outbox, uid, now, scope
from .crypto import canonical, digest


def audit(db, actor, action, resource_id, details=None):
    db.add(Audit(household_id=actor.household_id, owner_id=actor.user_id, action=action, resource_id=resource_id, details=json.dumps(details or {}, ensure_ascii=False)))


def event_metadata(db, kind, resource_id):
    metadata = {"automation_chain": json.dumps(db.info.get("automation_chain", []))}
    if kind.startswith("record."):
        record = db.get(Record, resource_id)
        if record:
            metadata.update(record_kind=record.kind, record_source=record.source,
                            record_owner_id=record.owner_id, record_version=record.version)
    return metadata


def emit(db, actor, kind, resource_id):
    db.add(Outbox(household_id=actor.household_id, owner_id=actor.user_id, kind=kind,
                 resource_id=resource_id, **event_metadata(db, kind, resource_id)))


def accessible(db, actor, include_deleted=False):
    scope(db, actor.user_id, actor.household_id)
    granted = select(Grant.record_id).where(Grant.grantee_id == actor.user_id, Grant.household_id == actor.household_id)
    query = select(Record).where(Record.household_id == actor.household_id, or_(Record.owner_id == actor.user_id, and_(Record.id.in_(granted), Record.sensitivity != "SECRET")))
    return query if include_deleted else query.where(Record.deleted.is_(False))


def read_record(db, actor, record_id):
    item = db.scalar(accessible(db, actor).where(Record.id == record_id).execution_options(populate_existing=True))
    if not item:
        raise HTTPException(404, "数据不存在或未授权")
    return item


def serialize(record, vault):
    return {"id": record.id, "owner_id": record.owner_id, "source": record.source, "source_id": record.source_id, "kind": record.kind, "version": record.version, "sensitivity": record.sensitivity, "cloud_policy": record.cloud_policy, "deleted": record.deleted, "payload": {} if record.deleted else vault.open(record.payload, record.owner_id + ":record:" + record.id)}


def ingest(db, actor, item, vault):
    scope(db, actor.user_id, actor.household_id)
    from .sync_order import lock_changes, notify_recipients
    lock_changes(db)
    record = db.scalar(select(Record).where(Record.owner_id == actor.user_id, Record.source == item.source, Record.source_id == item.source_id).with_for_update())
    if record:
        if record.deleted:
            raise HTTPException(409, "已删除来源禁止自动恢复，请使用新的来源标识")
        if item.version < record.version:
            raise HTTPException(409, "来源版本已过期")
        if item.version == record.version:
            old = serialize(record, vault)
            if any(old[k] != getattr(item, k) for k in ("kind", "sensitivity", "cloud_policy", "payload")):
                raise HTTPException(409, "相同版本对应不同内容")
            return record
        db.add(Revision(household_id=actor.household_id, owner_id=actor.user_id, record_id=record.id, version=record.version, payload=record.payload))
    else:
        record = Record(id=uid(), household_id=actor.household_id, owner_id=actor.user_id, source=item.source, source_id=item.source_id)
        db.add(record)
    old_sensitivity = record.sensitivity if record.version is not None else None
    # 系统敏感数据不能由客户端自降为公开。
    sensitive = item.kind.split(".")[0] in {"health", "location", "photo", "contacts"}
    record.kind, record.version = item.kind, item.version
    record.sensitivity = "SECRET" if item.sensitivity == "SECRET" else ("SENSITIVE" if sensitive else item.sensitivity)
    record.cloud_policy = "LOCAL_ONLY" if sensitive else item.cloud_policy
    record.payload = vault.seal(item.payload, actor.user_id + ":record:" + record.id)
    record.updated_at = now()
    emit(db, actor, "record.changed", record.id)
    notify_recipients(db, actor, "record.changed", record.id)
    if record.sensitivity == 'SECRET' and old_sensitivity != 'SECRET':
        from .sync_order import invalidate_snapshots
        recipients = list(db.scalars(select(Grant.grantee_id).where(Grant.record_id == record.id, Grant.owner_id == actor.user_id)))
        invalidate_snapshots(db, actor, recipients)
    audit(db, actor, "data.write", record.id)
    db.flush()
    return record
