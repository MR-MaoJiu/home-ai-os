"""家庭可见资料需要所有者明确授权；聊天记忆始终为个人内容。"""
from typing import Literal
from fastapi import APIRouter,Depends,Request,HTTPException
from pydantic import BaseModel,ConfigDict
from sqlalchemy import select
from .db import Record,Principal,SharingRule,scope
from .security import authenticate,own
from .data import audit,emit
from .sync_order import lock_changes,notify_recipients,invalidate_snapshots
router=APIRouter(prefix='/api/v1',tags=['个人与家庭范围'])
class VisibilityInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    visibility:Literal['personal','family']

def change_record(db,actor,record,value):
    if record.deleted:raise HTTPException(404,'资料已删除')
    if value=='family' and (record.kind.startswith('memory.') or record.sensitivity=='SECRET'):raise HTTPException(403,'聊天记忆和秘密资料不能设为家庭资料')
    previous=record.visibility
    if previous==value:return
    members=list(db.scalars(select(Principal.id).where(Principal.household_id==actor.household_id,Principal.id!=actor.user_id)))
    record.visibility=value;db.flush()
    if value=='personal':invalidate_snapshots(db,actor,members)
    notify_recipients(db,actor,'record.changed' if value=='family' else 'record.revoked',record.id,members)
    emit(db,actor,'record.changed',record.id)
    audit(db,actor,'data.visibility',record.id,{'visibility':value})
    if record.kind=='document.file':
        for child in db.scalars(select(Record).where(Record.owner_id==actor.user_id,Record.source=='document_parse',Record.source_id==record.id,Record.deleted.is_(False))):
            change_record(db,actor,child,value)

@router.put('/data/{record_id}/visibility')
def visibility(record_id:str,body:VisibilityInput,request:Request,actor=Depends(authenticate)):
    with request.app.state.db() as db:
        scope(db,actor.user_id,actor.household_id);lock_changes(db)
        record=own(db,Record,record_id,actor)
        change_record(db,actor,record,body.visibility);db.commit()
        return {'visibility':record.visibility}

@router.get('/sharing/categories')
def categories(request:Request,actor=Depends(authenticate)):
    with request.app.state.db() as db:
        scope(db,actor.user_id,actor.household_id)
        enabled={r.category for r in db.scalars(select(SharingRule).where(SharingRule.owner_id==actor.user_id,SharingRule.grantee_id=='*'))}
        return {kind:'family' if kind in enabled else 'personal' for kind in ('health','location')}

@router.put('/sharing/categories/{category}')
def category(category:Literal['health','location'],body:VisibilityInput,request:Request,actor=Depends(authenticate)):
    with request.app.state.db() as db:
        scope(db,actor.user_id,actor.household_id);lock_changes(db)
        row=db.scalar(select(SharingRule).where(SharingRule.owner_id==actor.user_id,SharingRule.category==category,SharingRule.grantee_id=='*'))
        if body.visibility=='family' and not row:db.add(SharingRule(owner_id=actor.user_id,household_id=actor.household_id,category=category,grantee_id='*'))
        elif body.visibility=='personal' and row:db.delete(row)
        for record in db.scalars(select(Record).where(Record.owner_id==actor.user_id,Record.kind.like(category+'.%'),Record.deleted.is_(False),Record.sensitivity!='SECRET')):
            change_record(db,actor,record,body.visibility)
        audit(db,actor,'sharing.category',category,{'visibility':body.visibility});db.commit()
        return {'visibility':body.visibility}
