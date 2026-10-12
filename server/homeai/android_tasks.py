"""安卓任务取得与释放设备操作权；客户端只展示，人工接管使原任务失效。"""
from fastapi import HTTPException
from .android_gestures import gesture_barrier
from .android_workspace import _lock, _member_key, _required, _active_device, _pause, ai_packages, get_binding
from .data import audit, emit
from .db import Task, Principal, scope, now
from .security import Actor

TERMINAL = {'SUCCEEDED', 'FAILED', 'CANCELED', 'NEEDS_RECONCILIATION'}



def usage_projection(body, task_id):
    used = body.get('_android_task_id') == task_id and isinstance(body.get('_android_workspace_id'), str)
    return {'used_android': used, 'android_task_id': task_id if used else None,
            'android_workspace_id': body.get('_android_workspace_id') if used else None}

def assert_task_control(app, db, actor, body, task_id):
    expected = body.get('_android_epoch')
    if expected is None:
        return
    row = get_binding(app, db, actor)
    if not row or row.mode != 'ai' or row.epoch != expected or row.active_task_id != task_id:
        raise HTTPException(409, '原安卓任务已被本人接管或结束，旧计划不能继续；请重新提出任务')


async def acquire(app, actor, task_id, body):
    # 先检查 human，不能让一个新 AI 请求通过取消围栏打断本人正在触控的手势。
    with app.db() as db:
        row = _required(app, db, actor)
        if row.mode == 'human':
            raise HTTPException(409, '你正在接管安卓设备，AI 没有抢占控制；请交还 AI 后重试任务')
    async with gesture_barrier(app, actor), _lock(app, _member_key(actor)):
        with app.db() as db:
            _active_device(db, actor)
            row = _required(app, db, actor)
            current = db.get(Task, task_id, with_for_update=True)
            if not current or current.owner_id != actor.user_id or current.household_id != actor.household_id:
                raise HTTPException(404, '安卓任务不存在')
            if current.cancel_requested or current.status in TERMINAL:
                raise HTTPException(409, '安卓任务已经结束')
            if not ai_packages(row):
                raise HTTPException(403, '此设备尚未允许 AI 操作任何 App；本人仍可完整接管')
            canonical = app.vault.open(current.request, actor.user_id + ':task:' + task_id)
            if canonical.get('_android_epoch') is not None:
                legacy_unclaimed = (canonical.get('_android_task_id') is None and row.active_task_id is None
                    and row.mode == 'ai' and row.epoch == canonical['_android_epoch'])
                if not legacy_unclaimed:
                    assert_task_control(app, db, actor, canonical, task_id)
                    body.update({key: canonical[key] for key in ('_android_epoch', '_android_workspace_id', '_android_task_id') if key in canonical})
                    return row
            if row.mode == 'human':
                raise HTTPException(409, '你正在接管安卓设备，AI 没有抢占控制；请交还 AI 后重试任务')
            if row.active_task_id and row.active_task_id != task_id:
                other = db.get(Task, row.active_task_id)
                if other and other.status not in TERMINAL:
                    raise HTTPException(409, '另一个任务正在使用你的安卓设备，请等待完成或先取消该任务')
                _pause(row)
            if row.mode != 'ai':
                _pause(row)
                row.mode = 'ai'
            row.active_task_id = task_id
            row.updated_at = now()
            body.update(_android_epoch=row.epoch, _android_workspace_id=row.id, _android_task_id=task_id)
            current.request = app.vault.seal(body, actor.user_id + ':task:' + task_id)
            audit(db, actor, 'android.task.started', task_id, {'workspace_id': row.id, 'epoch': row.epoch})
            emit(db, actor, 'task.updated', task_id)
            emit(db, actor, 'android.task.started', task_id)
            db.commit()
            return row



def release_finished_in_session(app, db, actor, task):
    if task.status not in TERMINAL:
        return False
    row = get_binding(app, db, actor)
    if row is None:
        return False
    db.refresh(row, with_for_update=True)
    if row.active_task_id != task.id:
        return False
    if row.mode == 'ai':
        _pause(row)
    else:
        row.active_task_id = None
    audit(db, actor, 'android.task.ended', task.id, {'workspace_id': row.id, 'status': task.status})
    emit(db, actor, 'task.updated', task.id)
    emit(db, actor, 'android.task.ended', task.id)
    return True

async def release_finished(app, task_id, user_id):
    with app.db() as db:
        member = db.get(Principal, user_id)
        if member is None:
            return
        actor = Actor(user_id, member.household_id, 'android-task-cleanup', member.role)
        scope(db, user_id, member.household_id)
        task = db.get(Task, task_id)
        row = get_binding(app, db, actor)
        if not task or task.status not in TERMINAL or not row or row.active_task_id != task_id:
            return
    async with gesture_barrier(app, actor), _lock(app, _member_key(actor)):
        with app.db() as db:
            scope(db, actor.user_id, actor.household_id)
            row = get_binding(app, db, actor)
            task = db.get(Task, task_id)
            if not row or row.active_task_id != task_id or not task or task.status not in TERMINAL:
                return
            release_finished_in_session(app, db, actor, task)
            db.commit()
