"""任务步骤检查与显式人工核对。结果不明的外部操作绝不自动重放。"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field
from typing import Literal, Any
from sqlalchemy import select
from .contracts import Contract
from .db import Task, Invocation, Approval, now
from .security import Actor, authenticate, own
from .policy import CAPABILITIES
from .data import audit, emit

router = APIRouter(prefix='/api/v1/tasks', tags=['任务恢复'])


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
            if CAPABILITIES[invocation.capability][0] >= 3:
                if not approval:
                    raise HTTPException(409, '缺少原操作审批记录')
                approval.decision, approval.expires_at = 'PENDING', now() + 300
                task.status = 'AWAITING_APPROVAL'
        task.error = None
        audit(db, actor, 'task.reconcile', invocation.id, {'decision': body.decision})
        emit(db, actor, 'task.updated', task.id)
        db.commit()
        return {'status': task.status, 'confirmation_source': 'user_reconciliation'}
