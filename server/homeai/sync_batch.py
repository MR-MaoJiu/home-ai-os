"""批次确认与记录写入同事务，重试不能复活已经删除的数据。"""
from fastapi import HTTPException
from sqlalchemy import select
from .db import SyncReceipt,uid,scope
from .crypto import digest,canonical
from .data import ingest,serialize,read_record
from .sync_order import lock_changes


def apply_batch(db,actor,body,vault):
    scope(db,actor.user_id,actor.household_id);lock_changes(db)
    fingerprint=digest(canonical(body.model_dump()))
    receipt=None
    if body.batch_id:
        receipt=db.scalar(select(SyncReceipt).where(SyncReceipt.owner_id==actor.user_id,SyncReceipt.device_id==actor.device_id,SyncReceipt.batch_id==body.batch_id))
    if receipt:
        if receipt.request_hash!=fingerprint:raise HTTPException(409,'批次编号已绑定其他内容')
        accepted=vault.open(receipt.payload,actor.user_id+':sync-receipt:'+receipt.id)
        records=[];removed=[]
        for rid in accepted['versions']:
            try:records.append(serialize(read_record(db,actor,rid),vault))
            except HTTPException:removed.append(rid)
        return {'records':records,'removed_ids':removed,'acknowledged':accepted['count'],'accepted_versions':accepted['versions'],'batch_id':body.batch_id,'replayed':True}
    records=[serialize(ingest(db,actor,item,vault),vault) for item in body.records]
    versions={record['id']:record['version'] for record in records}
    if body.batch_id:
        receipt=SyncReceipt(id=uid(),owner_id=actor.user_id,household_id=actor.household_id,device_id=actor.device_id,batch_id=body.batch_id,request_hash=fingerprint,payload='')
        receipt.payload=vault.seal({'count':len(records),'versions':versions},actor.user_id+':sync-receipt:'+receipt.id)
        db.add(receipt)
    return {'records':records,'acknowledged':len(records),'accepted_versions':versions,'batch_id':body.batch_id,'replayed':False}
