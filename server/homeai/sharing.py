"""按资料类型持续共享，修改与数据写入使用相同事务锁。"""
from typing import Literal
from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy import select
from .db import SharingRule, Record, Grant, Principal, scope
from .security import authenticate
from .data import audit
from .sync_order import lock_changes, notify_recipients, invalidate_snapshots

router = APIRouter(prefix='/api/v1/sharing', tags=['持续共享'])
Category = Literal['health', 'location']


def apply_rules(db, actor, record):
    category = record.kind.split('.')[0]
    if category not in ('health', 'location') or record.sensitivity == 'SECRET':
        return
    rules = db.scalars(select(SharingRule).where(SharingRule.owner_id == actor.user_id, SharingRule.category == category))
    for rule in rules:
        grant = db.scalar(select(Grant).where(Grant.record_id == record.id, Grant.grantee_id == rule.grantee_id))
        if not grant:
            db.add(Grant(household_id=actor.household_id, owner_id=actor.user_id, record_id=record.id, grantee_id=rule.grantee_id))
    db.flush()


@router.get('/rules')
def listing(request: Request, actor=Depends(authenticate)):
    with request.app.state.db() as db:
        scope(db, actor.user_id, actor.household_id)
        return [{'category': r.category, 'grantee_id': r.grantee_id} for r in db.scalars(select(SharingRule).where(SharingRule.owner_id == actor.user_id))]


@router.put('/rules/{category}/{member_id}')
@router.delete('/rules/{category}/{member_id}')
def change(category: Category, member_id: str, request: Request, actor=Depends(authenticate)):
    with request.app.state.db() as db:
        scope(db, actor.user_id, actor.household_id)
        lock_changes(db)
        member = db.get(Principal, member_id)
        if not member or member.household_id != actor.household_id or member.id == actor.user_id:
            raise HTTPException(404, '共享成员不存在')
        rule = db.scalar(select(SharingRule).where(SharingRule.owner_id == actor.user_id, SharingRule.category == category, SharingRule.grantee_id == member_id))
        enabled = request.method == 'PUT'
        if enabled and not rule:
            db.add(SharingRule(owner_id=actor.user_id, household_id=actor.household_id, category=category, grantee_id=member_id))
        elif not enabled and rule:
            db.delete(rule)
        db.flush()
        records = list(db.scalars(select(Record).where(Record.owner_id == actor.user_id, Record.kind.like(category + '.%'))))
        if not enabled:
            invalidate_snapshots(db, actor, [member_id])
        for record in records:
            grant = db.scalar(select(Grant).where(Grant.record_id == record.id, Grant.grantee_id == member_id))
            if enabled and not record.deleted and record.sensitivity != 'SECRET' and not grant:
                db.add(Grant(owner_id=actor.user_id, household_id=actor.household_id, record_id=record.id, grantee_id=member_id))
                db.flush()
                notify_recipients(db, actor, 'record.changed', record.id, [member_id])
            elif not enabled and grant:
                db.delete(grant)
                db.flush()
                notify_recipients(db, actor, 'record.revoked', record.id, [member_id])
        audit(db, actor, 'sharing.enable' if enabled else 'sharing.revoke', member_id, {'category': category})
        db.commit()
        return {'enabled': enabled}
