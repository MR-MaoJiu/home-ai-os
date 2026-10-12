"""把有权限展示的回答增量持久化，断线重连按原任务继续读取。"""
import time
from fastapi import HTTPException
from .db import Task,Device,scope
from .data import emit
from .result_access import check_dependencies


def publisher(app,actor,task_id):
    written='';last=0.0
    async def publish(text):
        nonlocal written,last
        current=time.monotonic()
        if not text or text==written or (last and current-last<.25):return
        with app.db() as db:
            scope(db,actor.user_id,actor.household_id)
            task=db.get(Task,task_id);device=db.get(Device,actor.device_id)
            if not task or task.owner_id!=actor.user_id or not device or device.revoked or task.cancel_requested or task.status!='EXECUTING':
                raise HTTPException(409,'当前回答已停止')
            body=app.vault.open(task.request,actor.user_id+':task:'+task.id)
            check_dependencies(db,actor,body)
            task.result=app.vault.seal({'text':text,'_streaming':True},actor.user_id+':task-result:'+task.id)
            emit(db,actor,'task.updated',task.id);db.commit()
        written=text;last=current
    return publish
