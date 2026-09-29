"""固定分页快照、按设备确认和最新权限回读。"""
from fastapi import APIRouter,Depends,HTTPException,Request,Query
from sqlalchemy import select,func,delete
from pydantic import Field
from .contracts import Contract
from .db import SyncSnapshot,SyncCursor,Outbox,Record,scope,uid,now
from .security import Actor,authenticate,own
from .data import accessible,serialize,read_record
from .sync_order import lock_changes
from .crypto import canonical

router=APIRouter(prefix='/api/v1/sync',tags=['设备增量同步'])


# 直连的单次报文上限为 30 MiB；同步页保持更小，避免附件占满串行通道。
MAX_PAGE_BYTES = 256 * 1024
MAX_INLINE_PAYLOAD_BYTES = 8 * 1024
PAGE_OVERHEAD_BYTES = 2048
METADATA_KEYS = ('name', 'title', 'filename', 'mime_type', 'media_type', 'size', 'sha256',
                 'due_at', 'completed', 'timestamp', 'date', 'created_at', 'updated_at',
                 'start_date', 'end_date', 'duration', 'unit', 'value', 'latitude', 'longitude', 'accuracy')


def strip_attachments(value):
    if isinstance(value, dict):
        result = {}
        deferred = False
        for key, item in value.items():
            if key.lower().endswith('_base64') or key.lower() == 'base64' or (isinstance(item, str) and item.startswith('data:') and ';base64,' in item[:100]):
                deferred = True
                continue
            cleaned, removed = strip_attachments(item)
            result[key] = cleaned
            deferred = deferred or removed
        return result, deferred
    if isinstance(value, list):
        result = []
        deferred = False
        for item in value:
            cleaned, removed = strip_attachments(item)
            result.append(cleaned)
            deferred = deferred or removed
        return result, deferred
    return value, False


def display_metadata(payload):
    result = {}
    for key in METADATA_KEYS:
        value = payload.get(key)
        if not isinstance(value, (str, int, float, bool)) and value is not None:
            continue
        if key not in payload:
            continue
        if isinstance(value, str):
            value = value.encode('utf-8')[:512].decode('utf-8', errors='ignore')
        candidate = {**result, key: value}
        if len(canonical(candidate)) <= MAX_INLINE_PAYLOAD_BYTES // 2:
            result = candidate
    return result


def sync_projection(record, vault):
    """同步只给可展示的小型投影；完整正文保留在单条鉴权读取接口。"""
    result = serialize(record, vault)
    payload = result['payload']
    # 先排除最常见的顶层内嵌附件，避免为了判定大小重新编码大块 Base64。
    compact = {key: value for key, value in payload.items() if not key.lower().endswith('_base64') and key.lower() != 'base64'}
    deferred = len(compact) != len(payload)
    if len(canonical(compact)) > MAX_INLINE_PAYLOAD_BYTES:
        compact, deferred = display_metadata(compact), True
    else:
        compact, nested_deferred = strip_attachments(compact)
        deferred = deferred or nested_deferred
    result['payload'] = compact
    result['payload_deferred'] = deferred
    return result


def snapshot_ids(payload):
    # 兼容升级前保存完整记录对象的快照，但禁止继续使用其中的过期正文。
    return [item if isinstance(item, str) else item['id'] for item in payload]


def record_watermark(db, actor):
    return db.scalar(select(func.max(Outbox.id)).where(Outbox.owner_id == actor.user_id,
        Outbox.household_id == actor.household_id, Outbox.kind.like('record.%'))) or 0


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
        values=list(db.scalars(accessible(db,actor).with_only_columns(Record.id).order_by(Record.id)))
        cursor=device_cursor(db,actor)
        watermark=max(cursor.acknowledged,record_watermark(db,actor))
        row=SyncSnapshot(id=uid(),owner_id=actor.user_id,household_id=actor.household_id,device_id=actor.device_id,watermark=watermark,payload='',next_offset=0,expires_at=now()+1800)
        row.payload=app.vault.seal(values,actor.user_id+':sync-snapshot:'+row.id)
        db.add(row);db.commit()
        return {'snapshot_id':row.id,'watermark':watermark,'count':len(values),'expires_at':row.expires_at}


@router.get('/snapshot/{snapshot_id}')
def page(snapshot_id:str,request:Request,offset:int=Query(0,ge=0),limit:int=Query(100,ge=1,le=200),actor:Actor=Depends(authenticate)):
    app=request.app.state
    with app.db() as db:
        scope(db,actor.user_id,actor.household_id);lock_changes(db)
        row=own(db,SyncSnapshot,snapshot_id,actor)
        db.refresh(row,with_for_update=True)
        if row.device_id!=actor.device_id:raise HTTPException(403,'快照属于另一设备')
        if row.expires_at<=now():raise HTTPException(410,'快照已过期，请重新建立并替换本地缓存')
        if offset>row.next_offset:raise HTTPException(409,'必须顺序读取快照分页')
        identifiers=snapshot_ids(app.vault.open(row.payload,actor.user_id+':sync-snapshot:'+row.id))
        end=offset;records=[];removed=[];page_bytes=PAGE_OVERHEAD_BYTES
        for identifier in identifiers[offset:offset+limit]:
            try: projected=sync_projection(read_record(db,actor,identifier),app.vault)
            except HTTPException: projected=None
            size=len(canonical(projected if projected is not None else identifier))+1
            if page_bytes+size>MAX_PAGE_BYTES:
                if end==offset:raise HTTPException(413,'记录元数据超过同步页限制')
                break
            if projected is None:removed.append(identifier)
            else:records.append(projected)
            page_bytes+=size;end+=1
        row.next_offset=max(row.next_offset,end)
        if end==len(identifiers):
            cursor=device_cursor(db,actor);cursor.offered=max(cursor.offered,row.watermark)
        db.commit()
        return {'records':records,'removed_ids':removed,'next_offset':end,'done':end==len(identifiers),'watermark':row.watermark}



@router.get('/changes')
def changes(request:Request,after:int|None=Query(None,ge=0),limit:int=Query(100,ge=1,le=200),actor:Actor=Depends(authenticate)):
    app=request.app.state
    with app.db() as db:
        scope(db,actor.user_id,actor.household_id);lock_changes(db)
        cursor=device_cursor(db,actor)
        if not cursor.initialized:raise HTTPException(409,'需要先完成并确认初始化快照')
        start=cursor.acknowledged if after is None else after
        if start>cursor.offered:raise HTTPException(409,'不能跳过未提供的同步游标')
        # 其它任务/通知事件不会使没有数据变化的客户端反复写缓存、推进 ACK。
        high=max(start,record_watermark(db,actor))
        events=list(db.scalars(select(Outbox).where(Outbox.owner_id==actor.user_id,Outbox.household_id==actor.household_id,
            Outbox.id>start,Outbox.id<=high,Outbox.kind.like('record.%')).order_by(Outbox.id).limit(limit+1)))
        records=[];removed=[];seen=set();processed=0;last=start;page_bytes=PAGE_OVERHEAD_BYTES
        for event in events[:limit]:
            if event.resource_id not in seen:
                try: projected=sync_projection(read_record(db,actor,event.resource_id),app.vault)
                except HTTPException: projected=None
                size=len(canonical(projected if projected is not None else event.resource_id))+1
                if page_bytes+size>MAX_PAGE_BYTES:
                    if processed==0:raise HTTPException(413,'记录元数据超过同步页限制')
                    break
                if projected is None:removed.append(event.resource_id)
                else:records.append(projected)
                page_bytes+=size;seen.add(event.resource_id)
            processed+=1;last=event.id
        more=len(events)>processed
        offered=last if more else high
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
            values=snapshot_ids(request.app.state.vault.open(snapshot.payload,actor.user_id+':sync-snapshot:'+snapshot.id))
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
