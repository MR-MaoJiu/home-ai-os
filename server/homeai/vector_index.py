"""规范记忆的本地派生向量索引。索引失败不会改变规范事实。"""
import json,math,hashlib
from sqlalchemy import select,text,delete,literal,exists
from fastapi import HTTPException
from .db import Record,MemoryVector,scope,uid
from .security import Actor
from .data import read_record,serialize


def vector_value(response):
    values=response['data'][0]['embedding']
    if not isinstance(values,list) or not 1<=len(values)<=4096 or any(type(x) not in (int,float) or not math.isfinite(x) for x in values):raise ValueError('Embedding 格式无效')
    if not any(x != 0 for x in values):raise ValueError('Embedding 不能是零向量')
    return json.dumps(values,separators=(',',':'))


def model_revision(manifest):
    # 同一 Provider 改模型或版本后，旧向量不能与新查询混用。
    identity = {k: getattr(manifest, k) for k in ('id', 'version', 'model', 'endpoint', 'embedding_query_prefix', 'embedding_document_prefix')}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def current(row, record, manifest, vault):
    if not row or row.model != model_revision(manifest):
        return False
    metadata = vault.open(row.embedding, record.owner_id + ':vector:' + row.id)
    return metadata.get('version') == record.version


async def reconcile(app, user_id, household_id):
    with app.db() as db:
        scope(db, user_id, household_id)
        # 一个主体只允许一个重建进程；事务结束自动释放，不遗留死锁标志。
        if db.bind.dialect.name == 'postgresql':
            locked = db.scalar(text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"), {'key': 'vectors:' + user_id})
            if not locked:
                return
        actor = Actor(user_id, household_id, 'memory-indexer', 'service')
        eligible = select(Record.id).where(Record.owner_id == user_id, Record.deleted.is_(False), Record.kind == 'memory.fact', Record.sensitivity != 'SECRET')
        db.execute(delete(MemoryVector).where(MemoryVector.owner_id == user_id, MemoryVector.record_id.not_in(eligible)))
        try:
            manifest = app.registry.resolve(db, 'model.embed@v1', cloud=False)
        except HTTPException:
            db.commit()
            return
        await app.policy.check(actor, 'model.embed@v1')
        count = 0
        rows = db.execute(select(Record, MemoryVector).outerjoin(MemoryVector, MemoryVector.record_id == Record.id).where(Record.id.in_(eligible)).order_by(Record.id)).all()
        for record, row in rows:
            if current(row, record, manifest, app.vault):
                continue
            version = record.version
            content = json.dumps(serialize(record, app.vault)['payload'], ensure_ascii=False)
            response = await app.registry.invoke(db, actor, manifest, 'model.embed@v1', {'input': manifest.embedding_document_prefix + content}, 'embed:' + record.id + ':' + str(version))
            encoded = vector_value(response)
            # 网络请求期间可能发生修改、改密级或删除；落库前锁定并再次核对。
            db.refresh(record, with_for_update=True)
            if record.deleted or record.version != version or record.sensitivity == 'SECRET' or record.kind != 'memory.fact':
                continue
            if not row:
                row = MemoryVector(id=uid(), owner_id=user_id, household_id=household_id, record_id=record.id, model='', embedding='')
                db.add(row)
            row.model = model_revision(manifest)
            row.embedding = app.vault.seal({'version': version, 'dimensions': len(json.loads(encoded))}, user_id + ':vector:' + row.id)
            db.flush()
            if db.bind.dialect.name == 'postgresql':
                db.execute(text('UPDATE memory_vectors SET search_vector=CAST(:v AS vector) WHERE id=:id'), {'v': encoded, 'id': row.id})
            count += 1
            if count >= 50:
                break
        db.commit()


async def search(app,db,actor,query):
    if db.bind.dialect.name!='postgresql':return None
    try:manifest=app.registry.resolve(db,'model.embed@v1',cloud=False)
    except HTTPException:return None
    await app.policy.check(actor,'model.embed@v1')
    response=await app.registry.invoke(db,actor,manifest,'model.embed@v1',{'input':manifest.embedding_query_prefix + query},'search:'+uid())
    encoded=vector_value(response)
    rows=db.execute(text('''SELECT record_id FROM memory_vectors
      WHERE owner_id=:owner AND household_id=:household AND model=:model
        AND search_vector IS NOT NULL AND vector_dims(search_vector)=vector_dims(CAST(:v AS vector))
      ORDER BY search_vector <=> CAST(:v AS vector) LIMIT 20'''),{'owner':actor.user_id,'household':actor.household_id,'model':model_revision(manifest),'v':encoded}).all()
    result=[]
    for row in rows:
        try:record=read_record(db,actor,row.record_id)
        except HTTPException:continue
        vector = db.scalar(select(MemoryVector).where(MemoryVector.record_id == record.id))
        if record.kind == 'memory.fact' and record.sensitivity != 'SECRET' and current(vector, record, manifest, app.vault):
            result.append(serialize(record,app.vault))
    return result
