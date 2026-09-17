"""固定分页快照、按设备确认和最新权限回读。"""
from fastapi import APIRouter,Depends,HTTPException,Request,Query
from sqlalchemy import select,func,delete
from pydantic import Field
from .contracts import Contract
from .db import SyncSnapshot,SyncCursor,Outbox,scope,uid,now
from .security import Actor,authenticate,own
from .data import accessible,serialize,read_record
from .sync_order import lock_changes

router=APIRouter(prefix='/api/v1/sync',tags=['设备增量同步'])


def device_cursor(db,actor):
    row=db.scalar(select(SyncCursor).where(SyncCursor.owner_id==actor.user_id,SyncCursor.device_id==actor.device_id).with_for_update())
    if not row:
        row=SyncCursor(id=uid(),owner_id=actor.user_id,household_id=actor.household_id,device_id=actor.device_id,acknowledged=0,offered=0,initialized=False)
        db.add(row);db.flush()
    return row


@router.post('/snapshot')
def snapshot(request:Request,actor:Actor=Depends(authenticate)):
    app=request.app.state
    with app.db() as db:
        scope(db,actor.user_id,actor.household_id);lock_changes(db)
        db.execute(delete(SyncSnapshot).where(SyncSnapshot.owner_id==actor.user_id,SyncSnapshot.expires_at<now()))
        rows=list(db.scalars(accessible(db,actor).order_by(__import__('homeai.db',fromlist=['Record']).Record.id)))
        values=[serialize(record,app.vault) for record in rows]
        watermark=db.scalar(select(func.max(Outbox.id)).where(Outbox.owner_id==actor.user_id)) or 0
        row=SyncSnapshot(id=uid(),owner_id=actor.user_id,household_id=actor.household_id,device_id=actor.device_id,watermark=watermark,payload='',next_offset=0,expires_at=now()+1800)
        row.payload=app.vault.seal(values,actor.user_id+':sync-snapshot:'+row.id)
        db.add(row);device_cursor(db,actor);db.commit()
        return {'snapshot_id':row.id,'watermark':watermark,'count':len(values),'expires_at':row.expires_at}


@router.get('/snapshot/{snapshot_id}')
def page(snapshot_id:str,request:Request,offset:int=Query(0,ge=0),limit:int=Query(100,ge=1,le=200),actor:Actor=Depends(authenticate)):
    app=request.app.state
    with app.db() as db:
        row=own(db,SyncSnapshot,snapshot_id,actor)
        db.refresh(row,with_for_update=True)
        if row.device_id!=actor.device_id:raise HTTPException(403,'快照属于另一设备')
        if row.expires_at<=now():raise HTTPException(410,'快照已过期，请重新建立并替换本地缓存')
        if offset>row.next_offset:raise HTTPException(409,'必须顺序读取快照分页')
        values=app.vault.open(row.payload,actor.user_id+':sync-snapshot:'+row.id)
        end=min(offset+limit,len(values));records=[];removed=[]
        for cached in values[offset:end]:
            try:read_record(db,actor,cached['id'])
            except HTTPException:removed.append(cached['id']);continue
            records.append(cached)
        row.next_offset=max(row.next_offset,end)
        if end==len(values):
            cursor=device_cursor(db,actor);cursor.offered=max(cursor.offered,row.watermark)
        db.commit()
        return {'records':records,'removed_ids':removed,'next_offset':end,'done':end==len(values),'watermark':row.watermark}


@router.get('/changes')
def changes(request:Request,after:int|None=Query(None,ge=0),limit:int=Query(100,ge=1,le=200),actor:Actor=Depends(authenticate)):
    app=request.app.state
    with app.db() as db:
        scope(db,actor.user_id,actor.household_id);lock_changes(db)
        cursor=device_cursor(db,actor)
        if not cursor.initialized:raise HTTPException(409,'需要先完成并确认初始化快照')
        start=cursor.acknowledged if after is None else after
        if start>cursor.offered:raise HTTPException(409,'不能跳过未提供的同步游标')
        high=db.scalar(select(func.max(Outbox.id)).where(Outbox.owner_id==actor.user_id)) or 0
        events=list(db.scalars(select(Outbox).where(Outbox.owner_id==actor.user_id,Outbox.id>start,Outbox.id<=high,Outbox.kind.like('record.%')).order_by(Outbox.id).limit(limit+1)))
        more=len(events)>limit;events=events[:limit]
        records=[];removed=[]
        for rid in dict.fromkeys(event.resource_id for event in events):
            try:record=read_record(db,actor,rid)
            except HTTPException:removed.append(rid);continue
            records.append(serialize(record,app.vault))
        offered=events[-1].id if more else high
        cursor.offered=max(cursor.offered,offered);db.commit()
        return {'records':records,'removed_ids':removed,'next_cursor':offered,'has_more':more}


class Acknowledge(Contract):
    cursor:int=Field(ge=0)
    snapshot_id:str|None=None


@router.post('/ack')
def acknowledge(body:Acknowledge,request:Request,actor:Actor=Depends(authenticate)):
    with request.app.state.db() as db:
        scope(db,actor.user_id,actor.household_id)
        row=device_cursor(db,actor)
        if body.cursor<row.acknowledged or body.cursor>row.offered:raise HTTPException(409,'游标回退或超出已提供范围')
        if body.snapshot_id:
            snapshot=own(db,SyncSnapshot,body.snapshot_id,actor)
            if snapshot.device_id!=actor.device_id or snapshot.expires_at<=now():raise HTTPException(409,'快照无效')
            values=request.app.state.vault.open(snapshot.payload,actor.user_id+':sync-snapshot:'+snapshot.id)
            if snapshot.next_offset!=len(values) or snapshot.watermark!=body.cursor:raise HTTPException(409,'快照尚未完整读取')
            row.initialized=True
        elif not row.initialized:raise HTTPException(409,'需要确认初始化快照')
        row.acknowledged=body.cursor;db.commit()
        return {'acknowledged_cursor':row.acknowledged}


class SourceLookup(Contract):
    source:str=Field(min_length=1,max_length=100)
    source_id:str=Field(min_length=1,max_length=200)


@router.post('/source')
def source_version(body:SourceLookup,request:Request,actor:Actor=Depends(authenticate)):
    from .db import Record
    with request.app.state.db() as db:
        scope(db,actor.user_id,actor.household_id)
        record=db.scalar(select(Record).where(Record.owner_id==actor.user_id,Record.source==body.source,Record.source_id==body.source_id))
        return {'version':record.version if record else 0,'deleted':record.deleted if record else False,'sensitivity':record.sensitivity if record else 'PRIVATE','cloud_policy':record.cloud_policy if record else 'LOCAL_ONLY'}
