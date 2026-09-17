"""文档语义检索只索引向量和范围，返回内容始终读取当前规范账本。"""
import json,re,hashlib
from fastapi import HTTPException
from sqlalchemy import select,delete,text
from .db import Record,KnowledgeChunk,scope,uid
from .security import Actor
from .data import accessible,read_record,serialize
from .vector_index import model_revision,vector_value
from .privacy import ensure_model_safe


def index_revision(manifest):
    return hashlib.sha256((model_revision(manifest)+':utf8-chunks-v1').encode()).hexdigest()


def chunks(content,size=1000,overlap=150,byte_limit=1800):
    if not 0<=overlap<size or byte_limit<4:raise ValueError('分块参数无效')
    start=0
    while start<len(content):
        end=start;used=0
        while end<len(content) and end-start<size:
            count=len(content[end].encode('utf8'))
            if used+count>byte_limit:break
            used+=count;end+=1
        yield start,end
        if end==len(content):break
        start=end-min(overlap,(end-start)//2)


async def reconcile(app,user_id,household_id):
    with app.db() as db:
        scope(db,user_id,household_id)
        if db.get_bind().dialect.name!='postgresql':return
        if not db.scalar(text('SELECT pg_try_advisory_xact_lock(hashtextextended(:key,0))'),{'key':'knowledge:'+user_id}):return
        eligible=select(Record.id).where(Record.owner_id==user_id,Record.kind=='document.parsed',Record.deleted.is_(False),Record.sensitivity!='SECRET')
        db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.owner_id==user_id,KnowledgeChunk.record_id.not_in(eligible)))
        try:manifest=app.registry.resolve(db,'model.embed@v1',cloud=False)
        except HTTPException:db.commit();return
        actor=Actor(user_id,household_id,'knowledge-indexer','service')
        await app.policy.check(actor,'model.embed@v1')
        revision=index_revision(manifest);processed=0
        byte_limit=1800-len(manifest.embedding_document_prefix.encode("utf8"))
        for record in db.scalars(select(Record).where(Record.id.in_(eligible)).order_by(Record.id)).all():
            db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.record_id==record.id, (KnowledgeChunk.version!=record.version)|(KnowledgeChunk.model!=revision)))
            content=serialize(record,app.vault)['payload'].get('markdown','')
            if not isinstance(content,str):continue
            try:ensure_model_safe(content)
            except HTTPException:
                db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.record_id==record.id))
                continue
            present=set(db.scalars(select(KnowledgeChunk.position).where(KnowledgeChunk.record_id==record.id,KnowledgeChunk.version==record.version,KnowledgeChunk.model==revision)))
            version=record.version
            for position,(start,end) in enumerate(chunks(content,byte_limit=byte_limit)):
                if position in present:continue
                response=await app.registry.invoke(db,actor,manifest,'model.embed@v1',{'input':manifest.embedding_document_prefix+content[start:end]},'document-embed:'+record.id+':'+str(version)+':'+str(position))
                vector=vector_value(response)
                db.refresh(record)
                if record.deleted or record.version!=version or record.sensitivity=='SECRET':break
                row=KnowledgeChunk(id=uid(),owner_id=user_id,household_id=household_id,record_id=record.id,version=version,model=revision,position=position,start=start,end=end)
                db.add(row);db.flush()
                db.execute(text('UPDATE knowledge_chunks SET search_vector=CAST(:v AS vector) WHERE id=:id'),{'v':vector,'id':row.id})
                processed+=1
                if processed>=32:db.commit();return
        db.commit()


def rehydrate(db,actor,result,vault):
    matches=[]
    for match in result.get('matches',[])[:5]:
        record=read_record(db,actor,match['record_id'])
        if record.kind!='document.parsed' or record.sensitivity=='SECRET':raise HTTPException(403,'文档不能进入模型')
        if record.version!=match['version']:raise HTTPException(409,'文档已变化，请重新检索')
        payload=serialize(record,vault)['payload'];content=payload.get('markdown','')
        ensure_model_safe(content)
        start,end=match['start'],match['end']
        if not isinstance(content,str) or not 0<=start<end<=len(content) or end-start>1000:raise HTTPException(502,'文档分块范围无效')
        title=payload.get('name','文档')
        matches.append({'record_id':record.id,'version':record.version,'start':start,'end':end,'title':title[:200] if isinstance(title,str) else '文档','excerpt':content[start:end]})
    return KnowledgeResponse(mode=result['mode'],matches=matches).model_dump()


async def search(app,db,actor,query):
    if not isinstance(query,str) or not query.strip() or len(query)>2000:raise HTTPException(422,'需要有效的文档查询')
    ensure_model_safe(query)
    matches=[];mode='authorized_literal'
    if db.get_bind().dialect.name=='postgresql':
        try:
            manifest=app.registry.resolve(db,'model.embed@v1',cloud=False)
            await app.policy.check(actor,'model.embed@v1')
            response=await app.registry.invoke(db,actor,manifest,'model.embed@v1',{'input':manifest.embedding_query_prefix+query},'document-query:'+uid())
            vector=vector_value(response)
            rows=db.execute(text('''SELECT c.record_id,c.version,c.start,c."end" FROM knowledge_chunks c
                JOIN data_records r ON r.id=c.record_id
                WHERE c.model=:model AND c.version=r.version AND NOT r.deleted
                  AND r.sensitivity<>'SECRET' AND r.kind='document.parsed' AND c.household_id=:household
                  AND vector_dims(c.search_vector)=vector_dims(CAST(:v AS vector))
                ORDER BY c.search_vector <=> CAST(:v AS vector) LIMIT 5'''),{'model':index_revision(manifest),'household':actor.household_id,'v':vector}).mappings().all()
            matches=[dict(row) for row in rows]
            if matches:mode='pgvector_chunks'
        except Exception:
            db.rollback();scope(db,actor.user_id,actor.household_id)
    if not matches:
        for record in db.scalars(accessible(db,actor).where(Record.kind=='document.parsed',Record.sensitivity!='SECRET').limit(1000)):
            content=serialize(record,app.vault)['payload'].get('markdown','')
            if not isinstance(content,str):continue
            try:ensure_model_safe(content)
            except HTTPException:continue
            found=re.search(re.escape(query),content,re.IGNORECASE)
            if not found:continue
            index=found.start()
            start=max(0,index-200);end=min(len(content),start+1000)
            matches.append({'record_id':record.id,'version':record.version,'start':start,'end':end})
            if len(matches)>=5:break
    return rehydrate(db,actor,{'mode':mode,'matches':matches},app.vault)


from fastapi import APIRouter,Depends,Request
from pydantic import Field
from .contracts import Contract
from .security import authenticate
router=APIRouter(prefix='/api/v1/knowledge',tags=['文档检索'])


from typing import Literal

class KnowledgeMatch(Contract):
    record_id:str
    version:int=Field(ge=1)
    start:int=Field(ge=0)
    end:int=Field(ge=1)
    title:str=Field(max_length=200)
    excerpt:str=Field(max_length=1000)

class KnowledgeResponse(Contract):
    mode:Literal['pgvector_chunks','authorized_literal']
    matches:list[KnowledgeMatch]=Field(max_length=5)

class SearchQuery(Contract):
    query:str=Field(min_length=1,max_length=2000)


@router.post('/search',response_model=KnowledgeResponse)
async def search_documents(body:SearchQuery,request:Request,actor:Actor=Depends(authenticate)):
    app=request.app.state
    with app.db() as db:
        scope(db,actor.user_id,actor.household_id)
        await app.policy.check(actor,'knowledge.search@v1')
        return await search(app,db,actor,body.query)


@router.post('/rebuild')
def rebuild(request:Request,actor:Actor=Depends(authenticate)):
    with request.app.state.db() as db:
        scope(db,actor.user_id,actor.household_id)
        db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.owner_id==actor.user_id))
        from .data import audit
        audit(db,actor,'knowledge.rebuild',actor.user_id)
        db.commit()
    return {'status':'PENDING','requires_worker':True}
