"""自动化由服务端持久执行；客户端仅提交意图和展示授权范围内的状态。"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from uuid import uuid5,NAMESPACE_URL
from croniter import croniter
from fastapi import HTTPException
from sqlalchemy import select,or_
from .db import Automation,AutomationDelivery,Device,Task,Principal,uid,now,scope
from .security import Actor
from .data import audit,emit


def executor(db,actor):
    identifier=str(uuid5(NAMESPACE_URL,'homeai:automation-service:'+actor.user_id))
    device=db.get(Device,identifier)
    if not device:
        db.add(Device(id=identifier,user_id=actor.user_id,public_key='',name='服务端自动化执行器'));db.flush()
    elif device.revoked:raise HTTPException(403,'服务端自动化执行身份已撤销')
    return Actor(actor.user_id,actor.household_id,identifier,actor.role)


def changed(db,actor,rule):
    emit(db,actor,'automation.updated',rule.id)
    if rule.visibility=='family':
        from .sync_order import notify_recipients
        members=list(db.scalars(select(Principal.id).where(Principal.household_id==actor.household_id)))
        notify_recipients(db,actor,'automation.updated',rule.id,members)


def create(app,db,actor,args,identifier=None):
    if set(args)-{'name','cron','instruction','visibility','timezone'}:raise HTTPException(422,'自动化参数包含不支持字段')
    name=args.get('name');instruction=args.get('instruction');visibility=args.get('visibility','personal');zone=args.get('timezone','Asia/Shanghai')
    if not isinstance(name,str) or not 1<=len(name)<=100 or not isinstance(instruction,str) or not 1<=len(instruction)<=4000 or visibility not in ('personal','family'):raise HTTPException(422,'自动化名称、指令或范围无效')
    try:
        expression=args['cron']
        if not isinstance(expression,str) or len(expression.split())!=5:raise ValueError()
        next_run=croniter(expression,datetime.now(ZoneInfo(zone))).get_next(float)
    except Exception:raise HTTPException(422,'需要五段 Cron 与有效时区') from None
    from .privacy import ensure_model_safe
    ensure_model_safe(instruction)
    actor=executor(db,actor);identifier=identifier or uid()
    row=db.get(Automation,identifier)
    if row:return row
    row=Automation(id=identifier,owner_id=actor.user_id,household_id=actor.household_id,name=name,cron=expression,timezone=zone,visibility=visibility,enabled=True,next_run=next_run,skill=app.vault.seal({'device_id':actor.device_id,'steps':[]},actor.user_id+':automation:'+identifier),instruction=app.vault.seal(instruction,actor.user_id+':automation-instruction:'+identifier))
    db.add(row);audit(db,actor,'automation.create',identifier,{'visibility':visibility});changed(db,actor,row);db.flush()
    return row


def visible(db,actor,identifier):
    scope(db,actor.user_id,actor.household_id)
    row=db.scalar(select(Automation).where(Automation.id==identifier,Automation.household_id==actor.household_id,or_(Automation.owner_id==actor.user_id,Automation.visibility=='family')))
    if not row:raise HTTPException(404,'自动化不存在或不属于当前范围')
    return row


def bind_task(app,task,rule):
    value=app.vault.open(task.request,rule.owner_id+':task:'+task.id)
    value.update(_automation_id=rule.id,_automation_scope=rule.visibility)
    task.request=app.vault.seal(value,rule.owner_id+':task:'+task.id)


def runs(app,db,actor,identifier):
    rule=visible(db,actor,identifier)
    owner_id=rule.owner_id
    try:
        scope(db,owner_id,actor.household_id)
        rows=db.scalars(select(AutomationDelivery).where(AutomationDelivery.automation_id==rule.id,AutomationDelivery.owner_id==owner_id).order_by(AutomationDelivery.created_at.desc()).limit(50))
        result=[]
        for row in rows:
            task=db.get(Task,row.task_id) if row.task_id else None
            summary=None
            if task and task.status=='SUCCEEDED' and task.result:
                try:
                    from .result_access import check_dependencies
                    from .db import Record
                    from .conversations import text_result
                    payload=app.vault.open(task.request,owner_id+':task:'+task.id)
                    execution_actor=Actor(owner_id,actor.household_id,payload['device_id'],'adult')
                    check_dependencies(db,execution_actor,payload)
                    dependencies=set(payload.get('record_ids',[]))|set(payload.get('_record_dependencies',{}))
                    if owner_id!=actor.user_id:
                        if payload.get('_automation_scope')!='family':raise HTTPException(403,'任务不是家庭执行')
                        for rid in dependencies:
                            record=db.get(Record,rid)
                            if not record or record.visibility!='family' or record.kind.startswith('memory.') or record.sensitivity=='SECRET':raise HTTPException(403,'任务引用非家庭资料')
                    summary=text_result(app.vault.open(task.result,owner_id+':task-result:'+task.id))
                except HTTPException:summary=None
            result.append({'id':row.id,'status':task.status if task else row.status,'created_at':row.created_at,'result_text':summary,**({'task_id':row.task_id} if owner_id==actor.user_id else {})})
        return result
    finally:scope(db,actor.user_id,actor.household_id)
