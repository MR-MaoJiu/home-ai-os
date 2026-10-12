"""人工和设备自动审核共用审批状态机，执行时仍重新校验全部权限。"""
from fastapi import HTTPException
from .data import audit, emit
from .db import Approval, Device, Invocation, Task, now
from .policy import capability_risk
from .security import own


def decide(app, db, actor, approval_id, decision, *, automatic=False):
    approval = own(db, Approval, approval_id, actor)
    invocation = own(db, Invocation, approval.invocation_id, actor)
    task = own(db, Task, invocation.task_id, actor)
    db.refresh(task, with_for_update=True)
    db.refresh(approval, with_for_update=True)
    if approval.decision != 'PENDING' or approval.expires_at <= now():
        raise HTTPException(409, '审批已处理或过期')
    if task.status != 'AWAITING_APPROVAL' or task.cancel_requested or task.deadline <= now():
        raise HTTPException(409, '任务不再等待审批')
    if decision == 'APPROVED':
        from .result_access import check_dependencies
        body = app.vault.open(task.request, actor.user_id + ':task:' + task.id)
        device = db.get(Device, body.get('device_id'))
        if not device or device.revoked or device.user_id != actor.user_id:
            raise HTTPException(409, '任务设备已撤销')
        if invocation.status != 'PENDING' or approval.arguments_hash != invocation.arguments_hash:
            raise HTTPException(409, '执行步骤或审批参数已变化')
        arguments = app.vault.open(invocation.arguments, actor.user_id + ':invocation:' + invocation.id)
        if capability_risk(invocation.capability, arguments) >= 4:
            raise HTTPException(403, '该操作不允许审批放行')
        check_dependencies(db, actor, body)
    approval.decision = decision
    task.status = 'APPROVED' if decision == 'APPROVED' else 'CANCELED'
    if decision == 'REJECTED':
        task.cancel_requested = True
    audit(db, actor, 'approval.' + decision.lower(), approval.id,
          {'automatic': True, 'device_id': actor.device_id} if automatic else None)
    emit(db, actor, 'task.updated', task.id)
    return {'status': task.status}
