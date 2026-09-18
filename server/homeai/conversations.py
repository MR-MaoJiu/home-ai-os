"""会话事实源在家庭服务器，客户端不能提交工具规划或伪造历史角色。"""
import json
from uuid import UUID,uuid5,NAMESPACE_URL
from fastapi import APIRouter,Depends,Request,HTTPException,Query
from pydantic import BaseModel,ConfigDict,Field,field_validator
from sqlalchemy import select,func
from .db import Conversation,ConversationTurn,Task,Invocation,Approval,uid,now,scope
from .security import authenticate,own
from .crypto import digest,canonical
from .contracts import TaskRequest
from .runtime import submit
from .result_access import check_dependencies

router=APIRouter(prefix='/api/v1',tags=['持久化会话'])
TERMINAL={'SUCCEEDED','FAILED','CANCELED','NEEDS_RECONCILIATION'}
class Input(BaseModel):model_config=ConfigDict(extra='forbid')
class Create(Input):client_id:UUID
class Message(Input):
    client_key:str=Field(min_length=8,max_length=100)
    content:str=Field(min_length=1,max_length=10000)
    timezone:str=Field(default='Asia/Shanghai',max_length=100)
    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls,value):return TaskRequest.valid_timezone(value)
class Voice(Input):
    client_key:str=Field(min_length=8,max_length=100)
    content_base64:str=Field(min_length=1,max_length=20000000)


def text_result(result):
    if not isinstance(result,dict):return ''
    if isinstance(result.get('text'),str):return result['text']
    value=result.get('choices',[{}])[0].get('message',{}).get('content') if result.get('choices') else None
    if isinstance(value,str):return value
    if result.get('status')=='stored':return '已保存到家庭服务器。'
    if result.get('content_trust')=='untrusted_web':return '已检索到公开资料：\n'+'\n'.join(str(item.get('title',''))[:200] for item in result.get('results',[])[:5])
    return ''


def task_result(app,db,actor,task):
    payload=app.vault.open(task.request,actor.user_id+':task:'+task.id)
    try:check_dependencies(db,actor,payload)
    except HTTPException:return None,True
    return app.vault.open(task.result,actor.user_id+':task-result:'+task.id) if task.result else None,False


def turn_view(app,db,actor,turn):
    task=db.get(Task,turn.task_id)
    result,redacted=task_result(app,db,actor,task)
    approvals=[]
    if task.status=='AWAITING_APPROVAL' and not task.cancel_requested:
        for approval,inv in db.execute(select(Approval,Invocation).join(Invocation,Approval.invocation_id==Invocation.id).where(Invocation.task_id==task.id,Approval.owner_id==actor.user_id,Approval.decision=='PENDING',Approval.expires_at>now())):
            approvals.append({'id':approval.id,'capability':inv.capability,'arguments':app.vault.open(inv.arguments,actor.user_id+':invocation:'+inv.id)})
    return {'id':turn.id,'sequence':turn.sequence,'client_key':turn.client_key,'user_text':app.vault.open(turn.user_message,actor.user_id+':chat-turn:'+turn.id),
            'task_id':task.id,'status':task.status,'assistant_text':'关联资料已删除或撤权，旧回答已隐藏。' if redacted else text_result(result),
            'error':None if redacted else task.error,'created_at':turn.created_at,'approvals':approvals,
            'sources':result.get('sources',[]) if isinstance(result,dict) else [],'web_sources':result.get('web_sources',result.get('results',[]) if result.get('content_trust')=='untrusted_web' else []) if isinstance(result,dict) else []}


@router.get('/conversations')
def listing(request:Request,offset:int=Query(default=0,ge=0,le=100000),actor=Depends(authenticate)):
    with request.app.state.db() as db:
        scope(db,actor.user_id,actor.household_id)
        rows=db.scalars(select(Conversation).where(Conversation.owner_id==actor.user_id).order_by(Conversation.updated_at.desc(),Conversation.id).offset(offset).limit(100))
        return [{'id':row.id,'title':request.app.state.vault.open(row.title,actor.user_id+':conversation:'+row.id),'updated_at':row.updated_at} for row in rows]

@router.post('/conversations')
def create(body:Create,request:Request,actor=Depends(authenticate)):
    app=request.app.state
    with app.db() as db:
        scope(db,actor.user_id,actor.household_id)
        identifier=str(body.client_id);existing=db.get(Conversation,identifier)
        if existing:return {'id':own(db,Conversation,identifier,actor).id}
        row=Conversation(id=identifier,owner_id=actor.user_id,household_id=actor.household_id,title=app.vault.seal('新对话',actor.user_id+':conversation:'+identifier))
        db.add(row);db.commit();return {'id':row.id}

@router.get('/conversations/{conversation_id}')
def read(conversation_id:str,request:Request,before:int|None=None,actor=Depends(authenticate)):
    app=request.app.state
    with app.db() as db:
        conversation=own(db,Conversation,conversation_id,actor)
        query=select(ConversationTurn).where(ConversationTurn.conversation_id==conversation.id,ConversationTurn.owner_id==actor.user_id)
        if before is not None:query=query.where(ConversationTurn.sequence<before)
        rows=list(db.scalars(query.order_by(ConversationTurn.sequence.desc()).limit(41)))
        more=len(rows)>40;rows=list(reversed(rows[:40]))
        return {'id':conversation.id,'title':app.vault.open(conversation.title,actor.user_id+':conversation:'+conversation.id),'turns':[turn_view(app,db,actor,row) for row in rows],'has_more':more,'next_before':rows[0].sequence if rows else None}

@router.post('/conversations/{conversation_id}/messages',status_code=202)
def message(conversation_id:str,body:Message,request:Request,actor=Depends(authenticate)):
    app=request.app.state
    with app.db() as db:
        conversation=own(db,Conversation,conversation_id,actor);db.refresh(conversation,with_for_update=True)
        fingerprint=digest(canonical(body.model_dump()))
        old=db.scalar(select(ConversationTurn).where(ConversationTurn.conversation_id==conversation.id,ConversationTurn.client_key==body.client_key))
        if old:
            if old.request_hash!=fingerprint:raise HTTPException(409,'同一发送标识不能用于不同消息')
            return turn_view(app,db,actor,old)
        latest=db.scalar(select(ConversationTurn).where(ConversationTurn.conversation_id==conversation.id).order_by(ConversationTurn.sequence.desc()).limit(1))
        if latest and db.get(Task,latest.task_id).status not in TERMINAL:raise HTTPException(409,'上一条消息仍在处理，请等待、确认或取消后继续')
        content=body.content.strip()
        if not content:raise HTTPException(422,'消息不能为空')
        task=submit(db,actor,TaskRequest(message=content,idempotency_key='chat:'+conversation.id+':'+body.client_key,timezone=body.timezone,max_model_tokens=131072),app.vault)
        conversation.next_sequence+=1;conversation.updated_at=now()
        if conversation.next_sequence==1:conversation.title=app.vault.seal(content[:40],actor.user_id+':conversation:'+conversation.id)
        turn=ConversationTurn(id=uid(),owner_id=actor.user_id,household_id=actor.household_id,conversation_id=conversation.id,sequence=conversation.next_sequence,client_key=body.client_key,request_hash=fingerprint,user_message=' ',task_id=task.id)
        turn.user_message=app.vault.seal(content,actor.user_id+':chat-turn:'+turn.id)
        payload=app.vault.open(task.request,actor.user_id+':task:'+task.id)
        payload.update(_conversation_id=conversation.id,_conversation_sequence=turn.sequence)
        task.request=app.vault.seal(payload,actor.user_id+':task:'+task.id)
        db.add(turn);db.commit();return turn_view(app,db,actor,turn)

@router.post('/input/voice',status_code=202)
def voice(body:Voice,request:Request,actor=Depends(authenticate)):
    with request.app.state.db() as db:
        task=submit(db,actor,TaskRequest(idempotency_key='voice:'+body.client_key,capability='speech.transcribe@v1',arguments={'content_base64':body.content_base64}),request.app.state.vault)
        db.commit();return {'id':task.id}


def history(app,db,actor,body):
    """上下文由服务端取最近历史，每轮重新校验旧答案的来源权限。"""
    if not body.get('_conversation_id'):return []
    own(db,Conversation,body['_conversation_id'],actor)
    rows=list(db.scalars(select(ConversationTurn).where(ConversationTurn.conversation_id==body['_conversation_id'],ConversationTurn.sequence<body['_conversation_sequence'],ConversationTurn.owner_id==actor.user_id).order_by(ConversationTurn.sequence.desc()).limit(12)))
    pairs=[];size=0
    for turn in rows:
        task=db.get(Task,turn.task_id)
        if not task or task.status not in TERMINAL:continue
        result,redacted=task_result(app,db,actor,task)
        if redacted:continue
        user=app.vault.open(turn.user_message,actor.user_id+':chat-turn:'+turn.id);answer=text_result(result) or ('上一轮未完成：'+(task.error or task.status))
        cost=len((user+answer).encode())
        if size+cost>8000:break
        size+=cost;pairs.append([{'role':'user','content':user},{'role':'assistant','content':answer}])
        old=app.vault.open(task.request,actor.user_id+':task:'+task.id)
        body.setdefault('_record_dependencies',{}).update(old.get('_record_dependencies',{}))
    return [message for pair in reversed(pairs) for message in pair]


def import_legacy(app,db,actor):
    """旧任务没有多轮会话标识，按独立历史对话迁移，不推断它们的关联。"""
    scope(db,actor.user_id,actor.household_id);count=0
    rows=db.scalars(select(Task).outerjoin(ConversationTurn,ConversationTurn.task_id==Task.id).where(Task.owner_id==actor.user_id,ConversationTurn.id.is_(None)).order_by(Task.created_at))
    for task in rows:
        body=app.vault.open(task.request,actor.user_id+':task:'+task.id)
        if body.get('_conversation_id') or task.idempotency_key.startswith('auto:'):continue
        message=body.get('message','')
        if not message and body.get('capability')=='web.search@v1':message='联网搜索：'+str(body.get('arguments',{}).get('query',''))
        if not message and body.get('capability')=='reminder.create@v1':message='创建提醒：'+str(body.get('arguments',{}).get('title',''))
        if not message:continue
        if not body.get('_agent') and (body.get('steps') or body.get('capability') not in (None,'model.generate@v1','web.search@v1','reminder.create@v1')):continue
        identifier=str(uuid5(NAMESPACE_URL,'homeai:legacy-chat:'+task.id));turn_id=uid()
        conversation=Conversation(id=identifier,owner_id=actor.user_id,household_id=actor.household_id,title=app.vault.seal('历史：'+message[:32],actor.user_id+':conversation:'+identifier),created_at=task.created_at,updated_at=task.created_at,next_sequence=1)
        turn=ConversationTurn(id=turn_id,owner_id=actor.user_id,household_id=actor.household_id,conversation_id=identifier,sequence=1,client_key='legacy:'+task.id,request_hash=task.request_hash,user_message=app.vault.seal(message,actor.user_id+':chat-turn:'+turn_id),task_id=task.id,created_at=task.created_at)
        db.add_all([conversation,turn]);count+=1
    db.commit();return count


class ReminderInput(Input):
    idempotency_key:str=Field(min_length=8,max_length=100)
    title:str=Field(min_length=1,max_length=500)
    due_at:str|None=None
    notify_at_due:bool=False
    timezone:str=Field(default='Asia/Shanghai',max_length=100)
    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls,value):return TaskRequest.valid_timezone(value)

@router.post('/input/reminder',status_code=202)
def reminder_intent(body:ReminderInput,request:Request,actor=Depends(authenticate)):
    from .reminders import normalize
    app=request.app.state
    arguments={'title':body.title}
    if body.due_at is not None:arguments.update(due_at=body.due_at,notify_at_due=body.notify_at_due)
    elif body.notify_at_due:raise HTTPException(422,'通知需要明确的到期时间')
    arguments=normalize(arguments)
    identifier=str(uuid5(NAMESPACE_URL,'homeai:reminder-intent:'+actor.user_id+':'+body.idempotency_key))
    with app.db() as db:
        scope(db,actor.user_id,actor.household_id)
        task=submit(db,actor,TaskRequest(idempotency_key='intent:'+body.idempotency_key,capability='reminder.create@v1',arguments=arguments,timezone=body.timezone),app.vault)
        if not db.get(Conversation,identifier):
            text='创建提醒：'+body.title+(('，到期时间：'+body.due_at) if body.due_at else '')
            turn_id=uid()
            db.add(Conversation(id=identifier,owner_id=actor.user_id,household_id=actor.household_id,title=app.vault.seal(text[:40],actor.user_id+':conversation:'+identifier),next_sequence=1))
            db.add(ConversationTurn(id=turn_id,owner_id=actor.user_id,household_id=actor.household_id,conversation_id=identifier,sequence=1,client_key=body.idempotency_key,request_hash=task.request_hash,user_message=app.vault.seal(text,actor.user_id+':chat-turn:'+turn_id),task_id=task.id))
        db.commit();return {'id':task.id,'conversation_id':identifier}
