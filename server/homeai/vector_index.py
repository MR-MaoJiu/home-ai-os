"""规范记忆的本地派生向量索引。索引失败不会改变规范事实。"""
import json,math
from sqlalchemy import select,text,delete,literal,exists
from fastapi import HTTPException
from .db import Record,MemoryVector,Outbox,Consumption,Principal,scope,uid
from .security import Actor
from .data import read_record,serialize


def vector_value(response):
    values=response['data'][0]['embedding']
    if not isinstance(values,list) or not 1<=len(values)<=4096 or any(not isinstance(x,(int,float)) or not math.isfinite(x) for x in values):raise ValueError('Embedding 格式无效')
    return json.dumps(values,separators=(',',':'))


async def reconcile(app,user_id,household_id):
    with app.db() as db:
        scope(db,user_id,household_id)
        actor=Actor(user_id,household_id,'memory-indexer','service')
        events=db.scalars(select(Outbox).where(Outbox.owner_id==user_id,Outbox.kind.in_(['record.changed','record.deleted']),~exists(select(Consumption.id).where(Consumption.id==literal('pgvector:')+Outbox.event_id))).order_by(Outbox.id).limit(50)).all()
        for event in events:
            marker='pgvector:'+event.event_id
            if db.get(Consumption,marker):continue
            record=db.get(Record,event.resource_id)
            if not record or record.deleted or record.kind!='memory.fact' or record.sensitivity=='SECRET':
                db.execute(delete(MemoryVector).where(MemoryVector.record_id==event.resource_id))
                db.add(Consumption(id=marker));db.commit();continue
            try:manifest=app.registry.resolve(db,'model.embed@v1',cloud=False)
            except HTTPException:return
            await app.policy.check(actor,'model.embed@v1')
            version=record.version
            content=json.dumps(serialize(record,app.vault)['payload'],ensure_ascii=False)
            response=await app.registry.invoke(db,actor,manifest,'model.embed@v1',{'input':content},'embed:'+record.id+':'+str(version))
            encoded=vector_value(response)
            db.refresh(record)
            if record.deleted or record.version!=version:
                db.rollback();continue
            row=db.scalar(select(MemoryVector).where(MemoryVector.record_id==record.id))
            if not row:
                row=MemoryVector(id=uid(),owner_id=user_id,household_id=household_id,record_id=record.id,model=manifest.id,embedding='')
                db.add(row)
            row.model=manifest.id
            row.embedding=app.vault.seal({'version':version,'dimensions':len(json.loads(encoded))},user_id+':vector:'+row.id)
            db.flush()
            if db.bind.dialect.name=='postgresql':db.execute(text('UPDATE memory_vectors SET search_vector=CAST(:v AS vector) WHERE id=:id'),{'v':encoded,'id':row.id})
            db.add(Consumption(id=marker));db.commit()


async def search(app,db,actor,query):
    if db.bind.dialect.name!='postgresql':return None
    try:manifest=app.registry.resolve(db,'model.embed@v1',cloud=False)
    except HTTPException:return None
    await app.policy.check(actor,'model.embed@v1')
    response=await app.registry.invoke(db,actor,manifest,'model.embed@v1',{'input':query},'search:'+uid())
    encoded=vector_value(response)
    rows=db.execute(text('''SELECT record_id FROM memory_vectors
      WHERE owner_id=:owner AND household_id=:household AND model=:model
        AND search_vector IS NOT NULL AND vector_dims(search_vector)=vector_dims(CAST(:v AS vector))
      ORDER BY search_vector <=> CAST(:v AS vector) LIMIT 20'''),{'owner':actor.user_id,'household':actor.household_id,'model':manifest.id,'v':encoded}).all()
    result=[]
    for row in rows:
        try:record=read_record(db,actor,row.record_id)
        except HTTPException:continue
        result.append(serialize(record,app.vault))
    return result
