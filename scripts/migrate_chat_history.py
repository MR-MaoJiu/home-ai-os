"""将旧单轮聊天任务保留为独立历史会话，不伪造不存在的多轮关系。"""
from sqlalchemy import select
from homeai.api import create_app
from homeai.db import Principal,Approval,Invocation,Task,scope,now
from homeai.security import Actor
from homeai.conversations import import_legacy
from homeai.data import audit,emit
app=create_app().state
count=0
with app.db() as db:
    users=list(db.scalars(select(Principal)))
    for user in users:
        actor=Actor(user.id,user.household_id,'migration',user.role)
        scope(db,user.id,user.household_id)
        for approval,inv,task in db.execute(select(Approval,Invocation,Task).join(Invocation,Approval.invocation_id==Invocation.id).join(Task,Invocation.task_id==Task.id).where(Approval.owner_id==user.id,Approval.decision=='PENDING',Invocation.capability=='web.search@v1',Task.status=='AWAITING_APPROVAL')):
            approval.decision='NOT_REQUIRED'
            task.status='RECEIVED' if task.deadline>now() else 'CANCELED'
            audit(db,actor,'search.approval_not_required',task.id);emit(db,actor,'task.updated',task.id)
        db.commit()
        count+=import_legacy(app,db,actor)
print('已保留旧单轮历史会话数量:',count)
