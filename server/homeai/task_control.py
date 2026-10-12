"""任务步骤检查与显式人工核对。结果不明的外部操作绝不自动重放。"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field
from typing import Literal, Any
from sqlalchemy import select
from .contracts import Contract
from .db import Task, Invocation, Approval, now
from .security import Actor, authenticate, own
from .policy import CAPABILITIES, capability_risk
from .data import audit, emit

router = APIRouter(prefix='/api/v1/tasks', tags=['任务恢复'])


def release_unnecessary_approvals(app, db, actor):
    """仅恢复仍有效且从未执行的普通操作；不把系统放行写成人工批准。"""
    rows = db.execute(select(Task, Invocation, Approval).join(Invocation, Invocation.task_id == Task.id)
        .join(Approval, Approval.invocation_id == Invocation.id).where(Task.owner_id == actor.user_id,
            Task.status == 'AWAITING_APPROVAL', Task.cancel_requested.is_(False), Task.deadline > now(),
            Invocation.owner_id == actor.user_id, Invocation.status == 'PENDING',
            Approval.owner_id == actor.user_id, Approval.decision == 'PENDING', Approval.expires_at > now())
        .with_for_update(skip_locked=True)).all()
    released = 0
    for task, invocation, approval in rows:
        arguments = app.vault.open(invocation.arguments, actor.user_id + ':invocation:' + invocation.id)
        if capability_risk(invocation.capability, arguments) >= 3:
            continue
        # 恢复后仍从 runtime 重新校验设备、参数、来源权限、隔离配置和 OPA。
        approval.decision = 'NOT_REQUIRED'
        task.status, task.error = 'RECEIVED', None
        audit(db, actor, 'approval.not_required', approval.id, {'capability': invocation.capability})
        emit(db, actor, 'task.updated', task.id)
        released += 1
    return released


@router.post('/{task_id}/resume')
def resume(task_id: str, request: Request, actor: Actor = Depends(authenticate)):
    app = request.app.state
    with app.db() as db:
        task = own(db, Task, task_id, actor)
        db.refresh(task, with_for_update=True)
        if task.status not in {'WAITING_BUDGET', 'WAITING_PRIVACY'} or task.cancel_requested:
            raise HTTPException(409, '只有预算或隐私服务暂停的任务可以重新检查')
        payload = app.vault.open(task.request, actor.user_id + ':task:' + task.id)
        if task.status == 'WAITING_BUDGET' and payload.get('_budget_failure', {}).get('recoverable') is False:
            raise HTTPException(409, task.error or '当前任务已达到固定限制，请缩小范围后重新提出任务')
        from .result_access import check_dependencies
        check_dependencies(db, actor, payload)
        from .privacy_review import reset_for_resume
        reset_for_resume(payload, task.status)
        payload.pop('_budget_failure', None)
        task.request = app.vault.seal(payload, actor.user_id + ':task:' + task.id)
        task.deadline, task.status, task.error = __import__('time').time() + min(payload.get('timeout_seconds', 600), 3600), 'RECEIVED', None
        emit(db, actor, 'task.updated', task.id)
        db.commit()
        return {'status': task.status}


class Reconciliation(Contract):
    decision: Literal['COMPLETED', 'NOT_EXECUTED', 'ABORT']
    evidence: str = Field(min_length=10, max_length=2000)
    result: dict[str, Any] = Field(default_factory=dict)


@router.get('/{task_id}/steps')
def steps(task_id: str, request: Request, actor: Actor = Depends(authenticate)):
    app = request.app.state
    with app.db() as db:
        task = own(db, Task, task_id, actor)
        from .result_access import check_dependencies
        try: check_dependencies(db, actor, app.vault.open(task.request, actor.user_id + ':task:' + task.id))
        except HTTPException: return []
        rows = db.scalars(select(Invocation).where(Invocation.task_id == task_id).order_by(Invocation.step))
        return [{'id': row.id, 'step': row.step, 'capability': row.capability, 'status': row.status, 'result': app.vault.open(row.result, actor.user_id + ':invocation-result:' + row.id) if row.result else None} for row in rows]


@router.post('/{task_id}/reconcile')
def reconcile(task_id: str, body: Reconciliation, request: Request, actor: Actor = Depends(authenticate)):
    app = request.app.state
    with app.db() as db:
        own(db, Task, task_id, actor)
        task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
        if task.status != 'NEEDS_RECONCILIATION':
            raise HTTPException(409, '任务不处于待核对状态')
        invocation = db.scalar(select(Invocation).where(Invocation.task_id == task_id, Invocation.status == 'NEEDS_RECONCILIATION').with_for_update())
        if not invocation:
            raise HTTPException(409, '找不到待核对步骤')
        # 核对说明可能包含外部凭据或业务内容，放入加密结果，不写普通审计字段。
        result = {'confirmation_source': 'user_reconciliation', 'evidence': body.evidence, 'result': body.result}
        invocation.result = app.vault.seal(result, actor.user_id + ':invocation-result:' + invocation.id)
        if body.decision == 'ABORT':
            task.status, invocation.status = 'FAILED', 'FAILED'
        elif body.decision == 'COMPLETED':
            invocation.status = 'SUCCEEDED'
            payload = app.vault.open(task.request, actor.user_id + ':task:' + task.id)
            count = len(payload.get('steps') or []) or 1
            task.status = 'CANCELED' if task.cancel_requested else ('RECEIVED' if payload.get('_agent') or invocation.step + 1 < count else 'SUCCEEDED')
            task.result = app.vault.seal(result, actor.user_id + ':task-result:' + task.id)
        else:
            from .result_access import check_dependencies
            check_dependencies(db, actor, app.vault.open(task.request, actor.user_id + ':task:' + task.id))
            if task.deadline <= now() or task.cancel_requested:
                raise HTTPException(409, '任务已过期，禁止重新发起外部操作')
            invocation.status = 'PENDING'
            task.status = 'RECEIVED'
            approval = db.scalar(select(Approval).where(Approval.invocation_id == invocation.id))
            if capability_risk(invocation.capability, app.vault.open(invocation.arguments, actor.user_id + ':invocation:' + invocation.id)) >= 3:
                if not approval:
                    raise HTTPException(409, '缺少原操作审批记录')
                approval.decision, approval.expires_at = 'PENDING', now() + 300
                task.status = 'AWAITING_APPROVAL'
        task.error = None
        audit(db, actor, 'task.reconcile', invocation.id, {'decision': body.decision})
        emit(db, actor, 'task.updated', task.id)
        db.commit()
        return {'status': task.status, 'confirmation_source': 'user_reconciliation'}


class RetryRequest(Contract):
    idempotency_key: str = Field(min_length=8, max_length=200)


def retry_status(app, db, actor, task, payload=None):
    """仅依据规范账本决定是否可恢复，失败副作用没有未派发证明时保持关闭。"""
    from .db import Device
    from .result_access import check_dependencies
    payload = payload if payload is not None else app.vault.open(task.request, actor.user_id + ':task:' + task.id)
    result = {'can_retry': False, 'code': 'not_failed', 'reason': '只有失败的任务可以重试', 'retry_step': None, 'completed_steps': 0}
    def blocked(code, reason):
        return {**result, 'code': code, 'reason': reason}
    if task.cancel_requested or task.status == 'CANCELED':
        return blocked('canceled', '任务已被取消，不能通过重试恢复')
    if task.status == 'NEEDS_RECONCILIATION':
        return blocked('needs_reconciliation', '外部操作结果不明，必须先人工核对，不能重试')
    if task.status != 'FAILED':
        return result
    if payload.get('_agent'):
        from .token_accounting import unsettled_cloud_planning, UNSETTLED_PLANNING_REASON
        if unsettled_cloud_planning(db, actor, task) is not None:
            return blocked('planning_result_unknown', UNSETTLED_PLANNING_REASON)
    device = db.get(Device, payload.get('device_id'))
    if not device or device.revoked or device.user_id != actor.user_id:
        return blocked('device_revoked', '原任务设备已撤销，不能恢复原执行身份；请重新提出任务')
    try:
        check_dependencies(db, actor, payload)
    except HTTPException as exc:
        return blocked('source_unavailable', str(exc.detail))
    from .client_actions import ClientAction
    from sqlalchemy import or_, and_
    refused = db.scalar(select(ClientAction.id).where(ClientAction.task_id == task.id, or_(
        ClientAction.status.in_(['DENIED', 'REVOKED', 'CANCELED', 'EXPIRED']),
        and_(ClientAction.status == 'PENDING', ClientAction.expires_at <= now()),
        and_(ClientAction.kind == 'cloud.disclose', ClientAction.status == 'RESPONDED', ClientAction.expires_at <= now()),
    )).limit(1))
    if refused:
        return blocked('authorization_ended', '所需资料或披露授权已被拒绝、撤回或过期；重试不能恢复授权，请重新提出任务')
    rows = list(db.scalars(select(Invocation).where(Invocation.task_id == task.id).order_by(Invocation.step)))
    result['completed_steps'] = sum(row.status == 'SUCCEEDED' for row in rows)
    steps = payload.get('steps') or []
    count = len(steps) if payload.get('_agent') else (len(steps) or 1)
    for row in rows:
        if row.owner_id != actor.user_id or row.household_id != actor.household_id or not 0 <= row.step < count:
            return blocked('invalid_ledger', '步骤归属或编号与原任务不一致，不能自动恢复')
        capability = CAPABILITIES.get(row.capability)
        if capability is None:
            return blocked('capability_removed', '原能力已不存在，不能自动恢复')
        if row.status in {'EXECUTING', 'NEEDS_RECONCILIATION'}:
            return blocked('unsettled_step', '仍有执行中或结果不明的步骤，不能重试')
        if row.status == 'CANCELED':
            return blocked('step_canceled', '已有步骤被明确取消，不能通过重试重新执行')
        if row.status not in {'SUCCEEDED', 'SKIPPED', 'UNAVAILABLE', 'FAILED', 'PENDING', 'RECEIVED'}:
            return blocked('unknown_step_state', '存在无法识别的步骤状态，不能自动恢复')
        if row.result and row.status != 'SUCCEEDED':
            value = app.vault.open(row.result, actor.user_id + ':invocation-result:' + row.id)
            if isinstance(value, dict) and value.get('confirmation_source') == 'user_reconciliation':
                return blocked('reconciliation_aborted', '人工核对已终止该操作，不能通过重试重新执行')
        if row.capability == 'model.generate@v1' and row.status not in {'SUCCEEDED', 'SKIPPED'}:
            from .model_routing import ModelUsage
            if db.scalar(select(ModelUsage.id).where(ModelUsage.call_key == row.id).limit(1)):
                return blocked('model_dispatch_reserved', '此云调用已有费用预留记录，不能重复发送；请先核对原调用结果')
        image_not_dispatched = False
        if row.capability in {'image.generate@v1', 'image.edit@v1'} and row.status in {'FAILED', 'PENDING'}:
            from .image_generation import ImageUsage
            reserved = db.scalar(select(ImageUsage.id).where(ImageUsage.call_key == row.id).limit(1))
            image_not_dispatched = reserved is None and payload.get('_image_dispatched') != row.id
            if not image_not_dispatched:
                return blocked('image_dispatch_reserved', '图片调用已有费用预留或派发记录，不能重复生成；请先核对外部结果')
        android_not_dispatched = (row.capability in {'android.launch@v1', 'android.action@v1'}
            and payload.get('_android_epoch') is None and payload.get('_android_dispatched') != row.id)
        if capability[1] and row.status not in {'SUCCEEDED', 'SKIPPED', 'PENDING'} and not image_not_dispatched and not android_not_dispatched:
            return blocked('effect_not_proven_safe', '失败的副作用步骤缺少“尚未执行”的可靠证明；请先核对结果，不能自动重试')
    if len(payload.get('_manual_retries', [])) >= 32:
        return blocked('retry_limit', '原任务已达到手动重试次数上限，请检查失败原因后重新提出范围明确的任务')
    retrying_search = any(row.capability == 'web.search@v1' and row.status in {'FAILED', 'UNAVAILABLE'} for row in rows)
    if payload.get('_agent'):
        active = count - sum(row.status == 'SKIPPED' for row in rows)
        if payload.get('_model_rounds', 0) >= payload.get('max_steps', 8) + 2 or (active >= payload.get('max_steps', 8) and payload.get('_final_round_used') and not retrying_search):
            return blocked('task_budget_exhausted', '原任务规划或步骤预算已用尽，重试不会重置预算；请缩小范围后重新提出任务')
        if payload.get('_model_token_charge', 0) + payload.get('max_output_tokens', 0) >= payload.get('max_model_tokens', 262144):
            return blocked('task_budget_exhausted', '原任务模型预算已用尽，重试不会重置已消耗额度')
    finished = {row.step for row in rows if row.status in {'SUCCEEDED', 'SKIPPED'}}
    next_step = next((index for index in range(count) if index not in finished), None)
    if next_step is not None:
        existing = next((row for row in rows if row.step == next_step), None)
        step = steps[next_step] if steps else payload
        capability = step.get('capability') or 'model.generate@v1'
        if capability not in CAPABILITIES or (existing and existing.capability != capability):
            return blocked('plan_changed', '步骤能力与原计划不一致，不能自动恢复')
        if existing:
            approval = db.scalar(select(Approval).where(Approval.invocation_id == existing.id))
            if approval and approval.decision in {'REJECTED', 'CANCELED'}:
                return blocked('approval_refused', '本次操作审批已拒绝或取消，重试不能替代新的授权')
    return {**result, 'can_retry': True, 'code': 'safe_retry', 'reason': '仅继续未完成部分；已成功步骤和已消耗预算保持不变。', 'retry_step': next_step}


@router.get('/{task_id}/retry')
def retry_preview(task_id: str, request: Request, actor: Actor = Depends(authenticate)):
    app = request.app.state
    with app.db() as db:
        task = own(db, Task, task_id, actor)
        return retry_status(app, db, actor, task)


@router.post('/{task_id}/retry')
def retry_task(task_id: str, body: RetryRequest, request: Request, actor: Actor = Depends(authenticate)):
    from .db import uid
    from sqlalchemy import text
    import hashlib
    app = request.app.state
    with app.db() as db:
        if db.get_bind().dialect.name == 'sqlite':
            db.execute(text('BEGIN IMMEDIATE'))
        task = own(db, Task, task_id, actor)
        payload = app.vault.open(task.request, actor.user_id + ':task:' + task.id)
        def replay():
            return any(item.get('key') == body.idempotency_key for item in payload.get('_manual_retries', []))
        if replay():
            return {'id': task.id, 'status': task.status, 'replayed': True}
        if db.get_bind().dialect.name == 'postgresql':
            key = int.from_bytes(hashlib.sha256(('task:' + task.id).encode()).digest()[:8], 'big', signed=True)
            if not db.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key': key}):
                raise HTTPException(409, '原任务仍在结束执行，请稍后重新检查')
        db.refresh(task, with_for_update=True)
        payload = app.vault.open(task.request, actor.user_id + ':task:' + task.id)
        if replay():
            return {'id': task.id, 'status': task.status, 'replayed': True}
        verdict = retry_status(app, db, actor, task, payload)
        if not verdict['can_retry']:
            raise HTTPException(409, verdict['reason'])
        step_number = verdict['retry_step']
        invocation = db.scalar(select(Invocation).where(Invocation.task_id == task.id, Invocation.step == step_number)) if step_number is not None else None
        history = {'key': body.idempotency_key, 'at': now(), 'step': step_number, 'previous_error': task.error,
                   'previous_status': invocation.status if invocation else None}
        if invocation and invocation.result:
            history['previous_result'] = invocation.result
        payload.setdefault('_manual_retries', []).append(history)
        if step_number is not None:
            reset_rows = list(db.scalars(select(Invocation).where(Invocation.task_id == task.id, Invocation.status.in_(['FAILED', 'UNAVAILABLE', 'RECEIVED']))))
            history['reset_steps'] = [{'id': row.id, 'step': row.step, 'status': row.status, 'result': row.result} for row in reset_rows]
            retrying_search = any(row.capability == 'web.search@v1' for row in reset_rows)
            for row in reset_rows:
                payload.get('_attempts', {}).pop(str(row.step), None)
                row.status, row.result = 'PENDING', None
            payload.get('_attempts', {}).pop(str(step_number), None)
            payload.pop('_draft_answer', None)
            payload.pop('_review_step_count', None)
            if retrying_search:
                # 撤下的是失败搜索的收尾控制，不返还已消耗的轮次、Token或成功工具回执。
                for key in ('_answer_from_existing', '_planner_feedback', '_final_round_used', '_duplicate_reads'):
                    payload.pop(key, None)
        # 一次隐私例外不能通过失败重试延长或重新使用。
        payload.pop('_privacy_review', None)
        payload.pop('_privacy_failure', None)
        task.status, task.error = 'RECEIVED', None
        task.deadline = now() + max(1, min(int(payload.get('timeout_seconds', 600)), 3600))
        if invocation and capability_risk(invocation.capability, app.vault.open(invocation.arguments, actor.user_id + ':invocation:' + invocation.id)) >= 3:
            approval = db.scalar(select(Approval).where(Approval.invocation_id == invocation.id))
            if approval:
                # 新审批使用新标识，旧设备缓存的“已批准”不能吞掉再次确认；历史留在加密重试账本。
                history['previous_approval'] = {'id': approval.id, 'decision': approval.decision,
                    'arguments_hash': approval.arguments_hash, 'expires_at': approval.expires_at}
                previous_id = approval.id
                approval.id, approval.decision, approval.expires_at = uid(), 'PENDING', now() + 300
                audit(db, actor, 'approval.renewed', approval.id, {'previous_id': previous_id})
            else:
                db.add(Approval(id=uid(), household_id=actor.household_id, owner_id=actor.user_id,
                    invocation_id=invocation.id, arguments_hash=invocation.arguments_hash, expires_at=now() + 300))
            task.status = 'AWAITING_APPROVAL'
            emit(db, actor, 'task.approval_required', task.id)
        task.request = app.vault.seal(payload, actor.user_id + ':task:' + task.id)
        audit(db, actor, 'task.retry', task.id, {'step': step_number, 'attempt': len(payload['_manual_retries'])})
        emit(db, actor, 'task.updated', task.id)
        db.commit()
        return {'id': task.id, 'status': task.status, 'replayed': False}
