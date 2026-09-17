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
        from .sync_order import lock_changes
        lock_changes(db)
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
        from .vector_index import search as vector_search
        try:
            semantic = await vector_search(app, db, actor, q)
            if semantic:
                return {'mode':'pgvector_exact','records':semantic}
        except Exception:
            db.rollback()
            scope(db,actor.user_id,actor.household_id)
        # 加密内容的基础检索在授权集合内执行；无向量 Provider 时不假装是语义检索。
        rows=db.scalars(accessible(db,actor).where(Record.kind=='memory.fact').limit(1000)).all()
        results=[serialize(r,app.vault) for r in rows if q.casefold() in json.dumps(serialize(r,app.vault)['payload'],ensure_ascii=False).casefold()]
        return {'mode':'authorized_literal','records':results[:50]}


@router.get('/index')
def index_status(request: Request, actor: Actor = Depends(authenticate)):
    from .vector_index import current
    app = request.app.state
    with app.db() as db:
        scope(db, actor.user_id, actor.household_id)
        try:
            manifest = app.registry.resolve(db, 'model.embed@v1', cloud=False)
        except HTTPException:
            manifest = None
        rows = db.execute(select(Record, MemoryVector).outerjoin(MemoryVector, MemoryVector.record_id == Record.id).where(Record.owner_id == actor.user_id, Record.deleted.is_(False), Record.kind == 'memory.fact', Record.sensitivity != 'SECRET')).all()
        ready = sum(bool(manifest and current(vector, record, manifest, app.vault)) for record, vector in rows)
        return {'provider_id': manifest.id if manifest else None, 'eligible': len(rows), 'ready': ready, 'pending': len(rows) - ready, 'status': 'UNCONFIGURED' if not manifest else ('READY' if ready == len(rows) else 'PENDING')}


@router.post('/index/rebuild')
def rebuild_index(request: Request, actor: Actor = Depends(authenticate)):
    # 仅丢弃调用者的派生数据。规范账本保持不变，工作进程自动补建。
    with request.app.state.db() as db:
        scope(db, actor.user_id, actor.household_id)
        db.execute(delete(MemoryVector).where(MemoryVector.owner_id == actor.user_id))
        audit(db, actor, 'memory.index.rebuild', actor.user_id)
        db.commit()
    return {'status': 'PENDING', 'requires_worker': True}


@router.get('/derived')
def derived_status(request: Request, actor: Actor = Depends(authenticate)):
    from .db import Provider, Secret
    from .contracts import ProviderManifest
    from .derived_memory import checkpoint, job_id
    with request.app.state.db() as db:
        scope(db, actor.user_id, actor.household_id)
        result = []
        for provider in db.scalars(select(Provider).order_by(Provider.id)):
            manifest = ProviderManifest.model_validate_json(provider.manifest)
            if not any(cap in manifest.capabilities for cap in ('memory.semantic.index@v1', 'memory.graph.index@v1')):
                continue
            if manifest.cloud:
                continue
            if manifest.secret_id:
                secret = db.get(Secret, manifest.secret_id)
                if not secret or secret.owner_id != actor.user_id or secret.provider_id != provider.id:
                    continue
            job = db.get(DerivedJob, job_id(actor.user_id, provider.id))
            status = 'DISABLED' if not provider.enabled else 'PENDING'
            if provider.enabled and job:
                status = job.status if job.event_id == checkpoint(db, actor.user_id, manifest) else 'PENDING'
            result.append({'provider_id': provider.id, 'status': status, 'attempts': job.attempts if job else 0, 'error_type': job.error if job else None})
        return result


@router.post('/derived/{provider_id}/rebuild')
def rebuild_derived(provider_id: str, request: Request, actor: Actor = Depends(authenticate)):
    from .db import Provider, Secret
    from .contracts import ProviderManifest
    from .derived_memory import checkpoint, job_id
    with request.app.state.db() as db:
        scope(db, actor.user_id, actor.household_id)
        provider = db.get(Provider, provider_id)
        if not provider:
            raise HTTPException(404, 'Provider 不存在')
        manifest = ProviderManifest.model_validate_json(provider.manifest)
        if manifest.cloud or not any({prefix + '.index@v1', prefix + '.purge@v1'} <= manifest.capabilities.keys() for prefix in ('memory.semantic', 'memory.graph')):
            raise HTTPException(422, '该 Provider 不是本地记忆投影')
        if manifest.secret_id:
            secret = db.get(Secret, manifest.secret_id)
            if not secret or secret.owner_id != actor.user_id or secret.provider_id != provider_id:
                raise HTTPException(403, '无权使用此 Provider 凭据')
        identifier = job_id(actor.user_id, provider_id)
        job = db.scalar(select(DerivedJob).where(DerivedJob.id == identifier).with_for_update())
        if not job:
            job = DerivedJob(id=identifier, owner_id=actor.user_id, household_id=actor.household_id, provider_id=provider_id, event_id=checkpoint(db, actor.user_id, manifest))
            db.add(job)
        job.status, job.error = 'PENDING', None
        audit(db, actor, 'memory.derived.rebuild', provider_id)
        db.commit()
        return {'status': 'PENDING', 'provider_enabled': provider.enabled, 'requires_worker': True}
