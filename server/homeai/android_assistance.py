"""安卓阻塞只暂停原任务；本人明确继续后重新观察，旧坐标永不重放。"""
import json

from fastapi import Depends, HTTPException, Request
from pydantic import Field
from sqlalchemy import select

from .contracts import Contract
from .crypto import canonical, digest
from .data import audit, emit
from .db import Approval, Device, Invocation, Task, now, scope, uid
from .security import Actor, authenticate
from .client_actions import (ClientAction, router, create_request, payload_of, _locked_action,
                             _check_response, _response_receipt, _response_notice, TERMINAL_TASKS)


def binding_fingerprint(row):
    return digest(canonical({'id': row.id, 'serial': row.serial, 'backend': row.backend,
                             'owner_id': row.owner_id, 'household_id': row.household_id,
                             'packages': row.packages}))


def clear_observation(body):
    for key in ('_android_focus', '_android_observation', '_android_manual_required'):
        body.pop(key, None)
    body['_android_needs_observation'] = True


def _not_dispatched(body, invocation):
    return invocation is None or body.get('_android_dispatched') != invocation.id


def withdraw_plan(app, db, actor, task, body, invocation, step_number, reason):
    """只撤回有可靠未派发证明的当前步骤和同批后续计划。"""
    if not _not_dispatched(body, invocation):
        raise HTTPException(409, '安卓输入已派发，必须先核对结果，不能重新执行')
    steps = body.get('steps') or []
    if not steps:
        steps = [{'capability': body.get('capability'), 'arguments': body.get('arguments', {})}]
        body['steps'] = steps
    indexes = list(range(step_number, len(steps)))
    for batch in body.get('_agent_batches', []):
        if step_number in batch.get('steps', []):
            indexes = [index for index in batch['steps'] if index >= step_number]
            break
    rows = {row.step: row for row in db.scalars(select(Invocation).where(Invocation.task_id == task.id))}
    for index in indexes:
        row = rows.get(index)
        if row and row.status not in {'PENDING', 'EXECUTING', 'WAITING_CLIENT'}:
            continue
        if row and row.status == 'EXECUTING' and (index != step_number or not _not_dispatched(body, row)):
            raise HTTPException(409, '已有安卓操作结果尚未核对，不能恢复旧计划')
        if row is None:
            step = steps[index]
            identifier = uid()
            args = step.get('arguments', {})
            row = Invocation(id=identifier, owner_id=actor.user_id, household_id=actor.household_id,
                task_id=task.id, step=index, capability=step['capability'], arguments_hash=digest(canonical(args)),
                arguments=app.vault.seal(args, actor.user_id + ':invocation:' + identifier))
            db.add(row)
        row.status = 'SKIPPED'
        row.result = app.vault.seal({'status': 'not_executed', 'reason': reason,
            'requires_replanning': True}, actor.user_id + ':invocation-result:' + row.id)
        for approval in db.scalars(select(Approval).where(Approval.invocation_id == row.id, Approval.decision == 'PENDING')):
            approval.decision = 'CANCELED'
    clear_observation(body)
    body['_planner_feedback'] = '先重新观察当前安卓页面，再继续原任务。此前标记not_executed的步骤没有执行，旧坐标和旧输入框已失效，已成功步骤不能重放。'


def append_observation(task, body):
    """Core 登记新的只读步骤，不能仅依赖模型是否遵守重新观察提示。"""
    index = len(body.setdefault('steps', []))
    args = {'purpose': '重新读取当前界面，继续原任务'}
    body['steps'].append({'capability': 'android.observe@v1', 'arguments': args})
    if body.get('_agent'):
        body.setdefault('_agent_batches', []).append({'steps': [index], 'message': {
            'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'call_' + task.id + '_' + str(index),
                'type': 'function', 'function': {'name': 'observe_android', 'arguments': json.dumps(args, ensure_ascii=False)}}]}})


async def recover_observation(app, db, actor, task, body, invocation, step_number, reason, code):
    db.refresh(task, with_for_update=True)
    if task.cancel_requested or task.status in TERMINAL_TASKS:
        return
    count = body.get('_android_observation_retries', 0) + 1
    body['_android_observation_retries'] = count
    if count >= 3:
        return await request_assistance(app, db, actor, task, body, invocation, step_number,
            '连续读取后仍无法确认可操作的界面，请处理当前页面后继续。', 'observation_unavailable')
    withdraw_plan(app, db, actor, task, body, invocation, step_number, code)
    if not body.get('_agent'):
        task.status, task.error = 'FAILED', '界面已变化，原安卓动作未执行；请重新提出任务。'
        task.request = app.vault.seal(body, actor.user_id + ':task:' + task.id)
        return
    append_observation(task, body)
    task.status, task.error = 'RECEIVED', None
    task.request = app.vault.seal(body, actor.user_id + ':task:' + task.id)
    audit(db, actor, 'android.observation.refresh', task.id, {'reason': code, 'attempt': count})


async def request_assistance(app, db, actor, task, body, invocation, step_number, reason, code):
    from .android_workspace import _lock, _member_key, _required, _pause
    if not _not_dispatched(body, invocation):
        raise HTTPException(409, '安卓输入已派发，必须先核对结果')
    # 不带任务行锁等待成员锁，避免与人工接管的锁顺序形成死锁。
    task.request = app.vault.seal(body, actor.user_id + ':task:' + task.id)
    db.commit()
    # 求助只释放 AI 控制，不触及真人手势；成员锁内已确认不是 human。
    async with _lock(app, _member_key(actor)):
        db.refresh(task, with_for_update=True)
        if task.cancel_requested or task.status in TERMINAL_TASKS:
            return
        row = _required(app, db, actor)
        if row.active_task_id not in {None, task.id} or row.mode == 'human':
            raise HTTPException(409, '安卓控制权已由其他操作取得，不能恢复此旧任务')
        withdraw_plan(app, db, actor, task, body, invocation, step_number, code)
        if not body.get('_agent') and [item.get('capability') for item in body.get('steps', [])] != ['android.observe@v1']:
            task.status, task.error = 'FAILED', reason + '；原动作未执行，处理后请重新提出任务。'
            task.request = app.vault.seal(body, actor.user_id + ':task:' + task.id)
            return
        fingerprint = binding_fingerprint(row)
        _pause(row)
        body.update(_android_task_id=task.id, _android_workspace_id=row.id)
        task.request = app.vault.seal(body, actor.user_id + ':task:' + task.id)
        result = create_request(app, db, actor, task, 'android.assist', reason,
            {'workspace_id': row.id, 'reason': reason, 'code': code},
            request_key='android-assist:' + str(step_number), _android_core=True)
        body = app.vault.open(task.request, actor.user_id + ':task:' + task.id)
        body['_android_assistance'] = {'action_id': result['client_action_id'], 'workspace_id': row.id,
            'binding_fingerprint': fingerprint, 'blocked_code': code}
        task.request = app.vault.seal(body, actor.user_id + ':task:' + task.id)
        task.error = None
        emit(db, actor, 'android.workspace.updated', row.id)
        audit(db, actor, 'android.assistance.request', task.id, {'reason': code})
        db.commit()


def _continuation_state(app, db, actor, action, task):
    from .android_workspace import _required, _expire, ai_packages
    if action.requester_id != actor.user_id or not task or task.owner_id != actor.user_id:
        raise HTTPException(403, '只能继续本人的安卓任务')
    saved = app.vault.open(task.request, actor.user_id + ':task:' + task.id)
    wait, assistance = saved.get('_client_wait', {}), saved.get('_android_assistance', {})
    if task.cancel_requested or task.status != 'WAITING_CLIENT' or wait.get('action_id') != action.id or assistance.get('action_id') != action.id:
        raise HTTPException(409, '原任务已取消、结束或等待条件已变化')
    device = db.get(Device, saved.get('device_id'))
    if not device or device.revoked or device.user_id != actor.user_id:
        raise HTTPException(403, '原任务发起设备已撤销')
    row = _required(app, db, actor)
    _expire(db, row)
    if row.id != assistance.get('workspace_id') or binding_fingerprint(row) != assistance.get('binding_fingerprint'):
        raise HTTPException(409, '安卓绑定或应用权限已变化，请重新提出任务')
    if not ai_packages(row):
        raise HTTPException(403, '尚未允许 AI 操作任何 App')
    if row.mode == 'human' and row.controller_device_id != actor.device_id:
        raise HTTPException(409, '另一台设备正在接管，请先在该设备结束接管')
    if row.active_task_id not in {None, task.id}:
        raise HTTPException(409, '另一个任务正在操作安卓设备')
    calls = list(db.scalars(select(Invocation).where(Invocation.task_id == task.id)))
    if any(call.status in {'EXECUTING', 'NEEDS_RECONCILIATION'} for call in calls):
        raise HTTPException(409, '原任务存在结果未核对的操作，不能直接继续')
    return saved, wait, row


class AndroidContinue(Contract):
    idempotency_key: str = Field(min_length=8, max_length=100)


@router.post('/client-actions/{action_id}/android-continue')
async def continue_android(action_id: str, body: AndroidContinue, request: Request, actor: Actor = Depends(authenticate)):
    from .android_gestures import gesture_barrier
    from .android_workspace import _lock, _member_key, _required, _active_device, _expire, _pause, ai_packages
    app = request.app.state
    fingerprint = digest(canonical({'decision': 'android-continue', **body.model_dump()}))
    with app.db() as db:
        scope(db, actor.user_id, actor.household_id)
        _active_device(db, actor)
        action, task = _locked_action(db, actor, action_id)
        if action.kind != 'android.assist':
            raise HTTPException(422, '此请求不是安卓协助')
        if _check_response(action, body.idempotency_key, fingerprint):
            return _response_receipt(app, action)
        _continuation_state(app, db, actor, action, task)
        db.rollback()
    async with gesture_barrier(app, actor), _lock(app, _member_key(actor)):
        with app.db() as db:
            scope(db, actor.user_id, actor.household_id)
            _active_device(db, actor)
            action, task = _locked_action(db, actor, action_id)
            if action.kind != 'android.assist':
                raise HTTPException(422, '此请求不是安卓协助')
            if _check_response(action, body.idempotency_key, fingerprint):
                return _response_receipt(app, action)
            saved, wait, row = _continuation_state(app, db, actor, action, task)
            _pause(row)
            row.mode, row.active_task_id = 'ai', task.id
            saved['_android_epoch'] = row.epoch
            clear_observation(saved)
            saved.pop('_android_assistance', None)
            saved.pop('_client_wait', None)
            saved['_android_observation_retries'] = 0
            append_observation(task, saved)
            task.status, task.error = 'RECEIVED', None
            task.deadline = now() + max(1, min(3600, wait['remaining_seconds']))
            task.request = app.vault.seal(saved, actor.user_id + ':task:' + task.id)
            action.status, action.responded_at = 'RESPONDED', now()
            action.response_key, action.response_hash = body.idempotency_key, fingerprint
            action.response = app.vault.seal({'id': action.id, 'status': 'RESPONDED',
                'note': '已交还 AI，将重新读取当前界面继续。'}, actor.user_id + ':client-response:' + action.id)
            _response_notice(db, actor, action)
            emit(db, actor, 'task.updated', task.id)
            emit(db, actor, 'android.workspace.updated', row.id)
            audit(db, actor, 'android.assistance.continue', task.id, {'action_id': action.id})
            db.commit()
            return _response_receipt(app, action)


def cancel_assistance(app, db, actor, action, task, *, expired=False):
    if not task or task.owner_id != actor.user_id or task.status != 'WAITING_CLIENT' or task.cancel_requested:
        raise HTTPException(409, '原安卓任务已结束或等待条件已变化')
    saved = app.vault.open(task.request, actor.user_id + ':task:' + task.id)
    if saved.get('_client_wait', {}).get('action_id') != action.id:
        raise HTTPException(409, '原安卓协助请求已变化')
    saved.pop('_client_wait', None)
    saved.pop('_android_assistance', None)
    task.cancel_requested = True
    task.status = 'FAILED' if expired else 'CANCELED'
    task.error = '安卓协助请求已过期' if expired else '你已取消安卓任务'
    task.request = app.vault.seal(saved, actor.user_id + ':task:' + task.id)
    emit(db, actor, 'task.updated', task.id)
