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
    enabled = db.scalar(select(SharingRule.id).where(SharingRule.owner_id == actor.user_id, SharingRule.category == category, SharingRule.grantee_id == '*'))
    if enabled:record.visibility='family'
    db.flush()



@router.get('/rules')
def listing(request: Request, actor=Depends(authenticate)):
    raise HTTPException(410,"请改用个人或家庭范围设置")

@router.put('/rules/{category}/{member_id}')
@router.delete('/rules/{category}/{member_id}')
def change(category: Category, member_id: str, request: Request, actor=Depends(authenticate)):
    raise HTTPException(410, "请改用个人或家庭范围设置")
