import json
import re
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, delete, text
from .db import MemoryCandidate, MemoryVector, DerivedJob, Record, uid, scope, now
from .security import Actor, authenticate, own
from .data import read_record, ingest, serialize, audit, accessible, emit
from .contracts import DataRecord

router = APIRouter(prefix='/api/v1/memory', tags=['记忆'])


class CandidateInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_ids: list[str] = Field(min_length=1,max_length=20)
    content: str = Field(min_length=1,max_length=20000)


@router.post('/candidates')
def candidate(body: CandidateInput, request: Request, actor: Actor = Depends(authenticate)):
    raise HTTPException(410,'记忆请从聊天生成，不能将上传资料直接作为聊天记忆')


@router.get('/candidates')
def candidates(request: Request, actor: Actor = Depends(authenticate)):
    with request.app.state.db() as db:
        scope(db,actor.user_id,actor.household_id)
        return [{'id':r.id,'status':r.status,'source_ids':json.loads(r.source_ids),'content':request.app.state.vault.open(r.content,actor.user_id+':candidate:'+r.id)} for r in db.scalars(select(MemoryCandidate).where(MemoryCandidate.owner_id==actor.user_id,MemoryCandidate.status=='PENDING',MemoryCandidate.conversation_id.is_not(None)))]




def save_chat_candidate(app,db,actor,item,*,require_current_source=False):
    if not item.conversation_id:
        raise HTTPException(410,'资料来源候选不能继续转换为聊天记忆，请从本人聊天重新提出')
    from .db import ConversationTurn
    from .auto_memory import resolve_statement,observe_turn,chat_source_context
    turn=own(db,ConversationTurn,item.turn_id,actor)
    source,observed=resolve_statement(app,db,actor,turn)
    if require_current_source and not observed:
        raise HTTPException(422,'请说明本次需要记住的本人信息，不能把助手回答或资料当成你的原话')
    if observed and observed['explicit']:
        result=observe_turn(app,db,actor,turn,chat_source_context(app,db,actor,turn))
        if result['status'] in {'SAVED','DUPLICATE'}:
            record=own(db,Record,result['record_id'],actor)
            if record.deleted:raise HTTPException(409,'这条记忆已经被遗忘')
            item.status='CONFIRMED'
            emit(db,actor,'memory.updated',record.id)
            return record
        if result['status']=='FORGOTTEN':raise HTTPException(409,'原声明已经被遗忘，请在新消息中明确提供需要重新记住的内容')
    sources=[own(db,Record,rid,actor) for rid in json.loads(item.source_ids)]
    if any(r.deleted for r in sources):raise HTTPException(409,'来源已删除，不能确认')
    content=app.vault.open(item.content,actor.user_id+':candidate:'+item.id)
    from .auto_memory import before_confirm, candidate_sensitivity, bind_confirmed, SENSITIVE, MemoryForgetTombstone, content_fingerprint
    from .privacy import PII,ensure_model_safe
    ensure_model_safe(content)
    if db.scalar(select(MemoryForgetTombstone.id).where(MemoryForgetTombstone.owner_id==actor.user_id,MemoryForgetTombstone.fingerprint==content_fingerprint(actor,content))):
        raise HTTPException(409,'此内容已被遗忘，不能从旧来源重新保存')
    before_confirm(app,db,actor,item)
    for existing in db.scalars(select(Record).where(Record.owner_id==actor.user_id,Record.kind=='memory.fact',Record.deleted.is_(False))):
        saved=app.vault.open(existing.payload,actor.user_id+':record:'+existing.id)
        if saved.get('content','').strip()==content.strip():
            item.status='CONFIRMED';bind_confirmed(db,actor,item,existing)
            emit(db,actor,'memory.updated',existing.id)
            return existing
    sensitivity=candidate_sensitivity(db,item.id,'SENSITIVE' if any(r.sensitivity=='SENSITIVE' for r in sources) or SENSITIVE.search(content) or PII.search(content) else 'PRIVATE')
    record=ingest(db,actor,DataRecord(source='conversation_memory' if item.conversation_id else 'memory_candidate',source_id=item.id,kind='memory.fact',version=1,sensitivity=sensitivity,payload={'content':content,'source_ids':[r.id for r in sources],'confirmed_at':now(),**({'conversation_id':item.conversation_id,'turn_id':item.turn_id} if item.conversation_id else {})}),app.vault)
    bind_confirmed(db,actor,item,record)
    item.status='CONFIRMED'
    audit(db,actor,'memory.confirm',record.id)
    emit(db,actor,'memory.updated',record.id)
    return record

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
            emit(db,actor,'memory.updated',item.id)
            item.content=request.app.state.vault.seal('',actor.user_id+':candidate:'+item.id)
            db.commit()
            return {'status':'REJECTED'}
        record=save_chat_candidate(request.app.state,db,actor,item)
        db.commit()
        return {'record_id':record.id,'status':'CONFIRMED'}


class EditMemory(BaseModel):
    model_config=ConfigDict(extra='forbid')
    content:str=Field(min_length=1,max_length=20000)
    version:int=Field(ge=1)


@router.patch('/entries/{record_id}')
def edit_entry(record_id:str,body:EditMemory,request:Request,actor:Actor=Depends(authenticate)):
    from .privacy import ensure_model_safe, PII
    from .auto_memory import remember_forget, SENSITIVE, addressing
    ensure_model_safe(body.content)
    app=request.app.state
    with app.db() as db:
        from .sync_order import lock_changes
        lock_changes(db)
        record=own(db,Record,record_id,actor);db.refresh(record,with_for_update=True)
        if record.deleted or not record.kind.startswith('memory.'):raise HTTPException(404,'个人聊天记忆不存在')
        if record.version!=body.version:raise HTTPException(409,'记忆已变化，请刷新后修改')
        remember_forget(app,db,actor,record)
        payload=serialize(record,app.vault)['payload']
        payload.update(content=body.content.strip(),origin='user_edited',edited_at=now())
        payload.pop('addressing',None)
        if names:=addressing(body.content):payload['addressing']=names
        sensitivity='SENSITIVE' if SENSITIVE.search(body.content) or PII.search(body.content) else record.sensitivity
        updated=ingest(db,actor,DataRecord(source=record.source,source_id=record.source_id,kind=record.kind,version=record.version+1,sensitivity=sensitivity,cloud_policy='LOCAL_ONLY' if sensitivity=='SENSITIVE' else record.cloud_policy,payload=payload),app.vault)
        emit(db,actor,'memory.updated',record.id);audit(db,actor,'memory.edit',record.id);db.commit()
        return serialize(updated,app.vault)


def memory_query(query):
    """限定主题的词项查询；不能把无主题的问题扩展成全量私人记忆。"""
    if not isinstance(query,str) or not 1<=len(query.strip())<=500:return None
    query=query.casefold().strip()
    fields=set()
    if query in {'preferred_name','assistant_name','response_language','response_length','response_tone'}:fields.add(query)
    if re.search(r'我(?:的)?(?:名字|姓名|昵称|称呼)|(?:怎么|什么)(?:称呼|叫)我|叫我什么|我叫什么|我是谁',query):fields.add('preferred_name')
    if re.search(r'你(?:的)?(?:名字|姓名|昵称|称呼)|(?:怎么|什么)(?:称呼|叫)你|叫你什么|你叫什么|你是谁',query):fields.add('assistant_name')
    if re.search(r'语言|用什么(?:话|文)|中文|英文|英语',query):fields.add('response_language')
    if re.search(r'回复.*(?:长短|长度)|回答.*(?:长短|长度)|简短|简洁|详细',query):fields.add('response_length')
    if re.search(r'语气|口吻|说话方式|温柔|口语化',query):fields.add('response_tone')
    aliases=set()
    for topic,terms in [(r'运动|锻炼|体育',('跑步','游泳','徒步','爬山','篮球','足球','健身')),
                        (r'饮料|饮品|喝什么|爱喝|喝的',('喝茶','咖啡','牛奶','绿茶','红茶','果汁','豆浆','汽水'))]:
        if re.search(topic,query):aliases.update(terms)
    noise=r'我想知道|之前提过的|长期记忆|还记不记得|还记得|记不记得|记忆|你知道|请问|告诉我|查询|检索|搜索|查一下|一下|帮我|关于|相关|什么|哪些|怎么|是否|有没有|喜欢|希望|偏好|习惯|以前|之前|平时|现在|记得|我的|你的|我们|你|我|的|吗|呢|啊|是|有|还'
    cleaned=re.sub(noise,' ',query)
    words={word for word in re.findall(r'[a-z0-9_-]+|[\u3400-\u9fff]+',cleaned) if len(word)>=2}
    words-={'what','which','who','the','is','are','my','your','do','does','please','remember','memory'}
    if re.fullmatch(r'[\u3400-\u9fff]',query) and query not in '你我他她它的吗呢啊是有还爱喜好':words.add(query)
    bigrams={word[index:index+2] for word in words if re.fullmatch(r'[\u3400-\u9fff]{3,}',word) for index in range(len(word)-1)}
    if not fields and not words and not aliases:return None
    return {'query':query,'fields':fields,'aliases':aliases,'words':words,'bigrams':bigrams}


def literal_search(app,db,actor,query,limit=8):
    """只在本人仍可访问的记忆正文中排序；来源元数据不参与匹配。"""
    from .auto_memory import addressing,memory_message
    terms=memory_query(query)
    if not terms:return []
    limit=max(1,min(limit,20))
    rows=db.scalars(accessible(db,actor).where(Record.owner_id==actor.user_id,Record.kind=='memory.fact',
        Record.sensitivity!='SECRET').order_by(Record.updated_at.desc(),Record.id).limit(1000))
    matches=[]
    for row in rows:
        serialized=serialize(row,app.vault);content=serialized['payload'].get('content','')
        if not isinstance(content,str) or not content.strip():continue
        text=content.casefold();score=0
        if terms['fields']:
            keys=set(addressing(content))
            observed=memory_message(content)
            if observed:keys.add(observed['statement']['key'])
            score=30*len(keys & terms['fields'])
            # 明确询问称呼或表达设置时，不返回仅含“我／你／记忆”的其他资料。
            if not score:continue
        elif terms['aliases']:
            score=sum(5 for word in terms['aliases'] if word in text)
            if not score:continue
        else:
            exact=[word for word in terms['words'] if word in text]
            partial=[word for word in terms['bigrams'] if word in text]
            if not exact and len(partial)<2:continue
            score=sum(min(len(word),8)*3 for word in exact)+len(partial)
        if terms['query'] in text:score+=50
        matches.append((score,row.updated_at,row.id,serialized))
    matches.sort(key=lambda item:(-item[0],-item[1],item[2]))
    return [item[3] for item in matches[:limit]]


@router.get('/search')
async def search(request: Request, q: str, actor: Actor = Depends(authenticate)):
    app=request.app.state
    with app.db() as db:
        scope(db,actor.user_id,actor.household_id)
        await app.policy.check(actor,'memory.search@v1')
        if not memory_query(q):return {'mode':'authorized_literal','records':[]}
        from .vector_index import search as vector_search
        try:
            semantic = await vector_search(app, db, actor, q)
            if semantic:
                return {'mode':'pgvector_exact','records':semantic}
        except Exception:
            db.rollback()
            scope(db,actor.user_id,actor.household_id)
        # 加密内容的基础检索在授权集合内执行；无向量 Provider 时不假装是语义检索。
        return {'mode':'authorized_literal','records':literal_search(app,db,actor,q)}


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


class ChatMemoryInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    conversation_id:str
    turn_id:str
    content:str=Field(min_length=1,max_length=20000)


def create_chat_candidate(app,db,actor,conversation_id,turn_id,content,identifier=None):
    from .db import Conversation,ConversationTurn
    from .privacy import ensure_model_safe
    conversation=own(db,Conversation,conversation_id,actor)
    turn=own(db,ConversationTurn,turn_id,actor)
    if turn.conversation_id!=conversation.id:raise HTTPException(422,'聊天轮次与会话不匹配')
    from .auto_memory import chat_source_context,resolve_statement
    chat_source_context(app,db,actor,turn)
    source,observed=resolve_statement(app,db,actor,turn)
    if observed:
        # 模型可以请求保存，但正文以本人声明为准，不能颠倒称呼或补写事实。
        content=observed['statement']['content']
    ensure_model_safe(content)
    identifier=identifier or uid()
    old=db.get(MemoryCandidate,identifier)
    if old:return old
    row=MemoryCandidate(id=identifier,household_id=actor.household_id,owner_id=actor.user_id,conversation_id=conversation.id,turn_id=turn.id,source_ids='[]',content=app.vault.seal(content,actor.user_id+':candidate:'+identifier))
    db.add(row);audit(db,actor,'memory.chat_candidate',row.id);emit(db,actor,'memory.updated',row.id);db.flush()
    return row

@router.post('/from-conversation')
def from_conversation(body:ChatMemoryInput,request:Request,actor=Depends(authenticate)):
    with request.app.state.db() as db:
        row=create_chat_candidate(request.app.state,db,actor,body.conversation_id,body.turn_id,body.content)
        db.commit();return {'id':row.id,'status':row.status}

@router.get('/entries')
def entries(request:Request,actor=Depends(authenticate)):
    with request.app.state.db() as db:
        scope(db,actor.user_id,actor.household_id)
        return [serialize(row,request.app.state.vault) for row in db.scalars(select(Record).where(Record.owner_id==actor.user_id,Record.kind.like('memory.%'),Record.deleted.is_(False)).order_by(Record.updated_at.desc()))]


@router.get('/entries/{record_id}')
def read_entry(record_id:str,request:Request,actor=Depends(authenticate)):
    with request.app.state.db() as db:
        record=own(db,Record,record_id,actor)
        if record.deleted or not record.kind.startswith('memory.'):
            raise HTTPException(404,'个人聊天记忆不存在')
        return serialize(record,request.app.state.vault)


@router.delete('/entries/{record_id}')
def forget_entry(record_id:str,request:Request,actor=Depends(authenticate)):
    app=request.app.state
    with app.db() as db:
        record=own(db,Record,record_id,actor)
        if record.deleted or not record.kind.startswith('memory.'):
            raise HTTPException(404,'个人聊天记忆不存在')
        from .deletion import delete_tree
        identifiers=delete_tree(db,actor,record,app.vault,app.settings.state_dir)
        emit(db,actor,'memory.updated',record.id)
        audit(db,actor,'memory.forget',record.id)
        db.commit()
        return {'deleted':True,'deleted_ids':identifiers,'derived_purge':'pending'}
