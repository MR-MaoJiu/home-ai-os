import json
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, delete, text
from .db import MemoryCandidate, MemoryVector, DerivedJob, Record, uid, scope, now
from .security import Actor, authenticate, own
from .data import read_record, ingest, serialize, audit, accessible
from .contracts import DataRecord

router = APIRouter(prefix='/api/v1/memory', tags=['记忆'])


class CandidateInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_ids: list[str] = Field(min_length=1,max_length=20)
    content: str = Field(min_length=1,max_length=20000)


@router.post('/candidates')
def candidate(body: CandidateInput, request: Request, actor: Actor = Depends(authenticate)):
    with request.app.state.db() as db:
        scope(db,actor.user_id,actor.household_id)
        for source in body.source_ids:
            record=own(db,Record,source,actor)
            if record.deleted or record.sensitivity=='SECRET':raise HTTPException(403,'来源不允许用于记忆')
        row=MemoryCandidate(id=uid(),household_id=actor.household_id,owner_id=actor.user_id,source_ids=json.dumps(body.source_ids),content='')
        row.content=request.app.state.vault.seal(body.content,actor.user_id+':candidate:'+row.id)
        db.add(row)
        db.commit()
        return {'id':row.id,'status':row.status}


@router.get('/candidates')
def candidates(request: Request, actor: Actor = Depends(authenticate)):
    with request.app.state.db() as db:
        scope(db,actor.user_id,actor.household_id)
        return [{'id':r.id,'status':r.status,'source_ids':json.loads(r.source_ids),'content':request.app.state.vault.open(r.content,actor.user_id+':candidate:'+r.id)} for r in db.scalars(select(MemoryCandidate).where(MemoryCandidate.owner_id==actor.user_id,MemoryCandidate.status=='PENDING'))]


@router.post('/candidates/{candidate_id}/{decision}')
def confirm(candidate_id: str, decision: str, request: Request, actor: Actor = Depends(authenticate)):
    with request.app.state.db() as db:
        item=own(db,MemoryCandidate,candidate_id,actor)
        if item.status!='PENDING':raise HTTPException(409,'候选已处理')
        if decision not in {'confirm','reject'}:raise HTTPException(422,'无效决定')
        if decision=='reject':
            item.status='REJECTED'
            item.content=request.app.state.vault.seal('',actor.user_id+':candidate:'+item.id)
            db.commit()
            return {'status':'REJECTED'}
        sources=[own(db,Record,rid,actor) for rid in json.loads(item.source_ids)]
        if any(r.deleted for r in sources):raise HTTPException(409,'来源已删除，不能确认')
        content=request.app.state.vault.open(item.content,actor.user_id+':candidate:'+item.id)
        record=ingest(db,actor,DataRecord(source='memory_candidate',source_id=item.id,kind='memory.fact',version=1,sensitivity='SENSITIVE' if any(r.sensitivity=='SENSITIVE' for r in sources) else 'PRIVATE',payload={'content':content,'source_ids':[r.id for r in sources],'confirmed_at':now()}),request.app.state.vault)
        item.status='CONFIRMED'
        audit(db,actor,'memory.confirm',record.id)
        db.commit()
        return {'record_id':record.id,'status':'CONFIRMED'}


@router.get('/search')
async def search(request: Request, q: str, actor: Actor = Depends(authenticate)):
    app=request.app.state
    with app.db() as db:
        scope(db,actor.user_id,actor.household_id)
        await app.policy.check(actor,'memory.search@v1')
        # 加密内容的基础检索在授权集合内执行；无向量 Provider 时不假装是语义检索。
        rows=db.scalars(accessible(db,actor).where(Record.kind=='memory.fact').limit(1000)).all()
        results=[serialize(r,app.vault) for r in rows if q.casefold() in json.dumps(serialize(r,app.vault)['payload'],ensure_ascii=False).casefold()]
        return {'mode':'authorized_literal','records':results[:50]}
