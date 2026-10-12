"""已配对设备的显式自动审核；只批准本人、该设备发起的有效请求。"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import StrictBool
from sqlalchemy import and_, or_, select
from .contracts import Contract
from .data import audit, emit
from .db import Approval, Device, Invocation, Principal, Task, now, scope
from .security import Actor, authenticate

router = APIRouter(prefix='/api/v1/devices/current', tags=['设备自动审核'])


class AutoReviewInput(Contract):
    enabled: StrictBool
    acknowledged: StrictBool = False


def current_device(db, actor, *, lock=False):
    query = select(Device).where(Device.id == actor.device_id, Device.user_id == actor.user_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    device = db.scalar(query)
    member = db.get(Principal, actor.user_id)
    if not device or device.revoked or not member or member.household_id != actor.household_id:
        raise HTTPException(403, '请使用本人已配对设备设置自动审核')
    return device


def view(device):
    return {'enabled': device.auto_review_enabled, 'device_id': device.id, 'scope': 'current_device_tasks'}


@router.get('/auto-review')
def get_setting(request: Request, actor: Actor = Depends(authenticate)):
    with request.app.state.db() as db:
        return view(current_device(db, actor))


@router.put('/auto-review')
def set_setting(body: AutoReviewInput, request: Request, actor: Actor = Depends(authenticate)):
    if body.enabled and not body.acknowledged:
        raise HTTPException(422, '开启自动审核需要先确认风险提示')
    with request.app.state.db() as db:
        scope(db, actor.user_id, actor.household_id)
        device = current_device(db, actor, lock=True)
        if device.auto_review_enabled != body.enabled:
            device.auto_review_enabled = body.enabled
            audit(db, actor, 'auto_review.enabled' if body.enabled else 'auto_review.disabled', device.id)
            emit(db, actor, 'device.auto_review.updated', device.id)
        result = view(device)
        db.commit()
        return result


def require_enabled(app, db, actor, task):
    """开关与每次决定使用同一设备行锁，关闭返回后不能再产生新的自动决定。"""
    device = current_device(db, actor, lock=True)
    if not device.auto_review_enabled:
        raise HTTPException(409, '自动审核已关闭')
    if not task or task.owner_id != actor.user_id or task.household_id != actor.household_id or task.cancel_requested:
        raise HTTPException(409, '原任务已失效')
    payload = app.vault.open(task.request, actor.user_id + ':task:' + task.id)
    if payload.get('device_id') != actor.device_id:
        raise HTTPException(403, '自动审核只适用于当前设备发起的任务')
    return payload


async def process_pending(app, user_id, household_id, role):
    """后台仅消费已有挑战，不运行工具，不猜测输入，也不自动重放结果不明的副作用。"""
    with app.db() as db:
        scope(db, user_id, household_id)
        enabled = set(db.scalars(select(Device.id).where(Device.user_id == user_id,
            Device.revoked.is_(False), Device.auto_review_enabled.is_(True))))
        if not enabled:
            return 0
        positions = getattr(app, 'auto_review_positions', None)
        if positions is None:
            positions = app.auto_review_positions = {}
        position = positions.get((user_id, household_id))
        query = select(Task.id, Task.status, Task.created_at).where(Task.owner_id == user_id,
            Task.household_id == household_id, Task.cancel_requested.is_(False),
            Task.status.in_(['AWAITING_APPROVAL', 'WAITING_PRIVACY', 'WAITING_CLIENT']))
        if position:
            query = query.where(or_(Task.created_at > position[0], and_(Task.created_at == position[0], Task.id > position[1])))
        candidates = list(db.execute(query.order_by(Task.created_at, Task.id).limit(100)))
        # 无法自动采集的旧待办不能一直占住首页，造成后续可批准任务饥饿。
        positions[(user_id, household_id)] = (candidates[-1][2], candidates[-1][0]) if len(candidates) == 100 else None
    completed = 0
    for identifier, expected_status, _ in candidates:
        try:
            with app.db() as db:
                from .sync_order import lock_changes
                scope(db, user_id, household_id)
                # 与人工客户端响应的锁顺序一致，避免数据同步锁和任务锁互相等待。
                if expected_status == 'WAITING_CLIENT':
                    lock_changes(db)
                task = db.scalar(select(Task).where(Task.id == identifier, Task.owner_id == user_id,
                    Task.household_id == household_id).with_for_update(skip_locked=True))
                if not task or task.cancel_requested or task.status != expected_status:
                    continue
                payload = app.vault.open(task.request, user_id + ':task:' + task.id)
                device_id = payload.get('device_id')
                if device_id not in enabled:
                    continue
                actor = Actor(user_id, household_id, device_id, role)
                require_enabled(app, db, actor, task)
                handled = await _process_task(app, db, actor, task, payload)
                if handled:
                    db.commit()
                    completed += 1
        except HTTPException:
            # 过期、撤权、硬性隐私限制或策略拒绝继续显示原卡片，不伪造同意或失败。
            continue
    return completed


async def _process_task(app, db, actor, task, payload):
    if task.status == 'AWAITING_APPROVAL':
        invocation = db.scalar(select(Invocation).where(Invocation.task_id == task.id,
            Invocation.owner_id == actor.user_id, Invocation.household_id == actor.household_id,
            Invocation.status == 'PENDING'))
        if not invocation:
            return False
        approval = db.scalar(select(Approval).where(Approval.invocation_id == invocation.id,
            Approval.owner_id == actor.user_id, Approval.household_id == actor.household_id,
            Approval.decision == 'PENDING', Approval.expires_at > now()))
        if not approval:
            return False
        arguments = app.vault.open(invocation.arguments, actor.user_id + ':invocation:' + invocation.id)
        await app.policy.check(actor, invocation.capability, True, arguments=arguments)
        from .approval_service import decide
        decide(app, db, actor, approval.id, 'APPROVED', automatic=True)
        return True
    if task.status == 'WAITING_PRIVACY':
        review = payload.get('_privacy_review') or {}
        if review.get('status') != 'pending' or review.get('overrideable') is not True:
            return False
        from .privacy_review import ReviewDecision, decide_in_transaction
        decide_in_transaction(app, db, actor, task.id, ReviewDecision(review_id=review['id'],
            decision='force', idempotency_key='auto-review:' + review['id']), automatic=True)
        return True
    if task.status == 'WAITING_CLIENT':
        from .client_actions import ClientAction, ResponseInput, payload_of, respond_in_transaction
        action_id = (payload.get('_client_wait') or {}).get('action_id')
        action = db.scalar(select(ClientAction).where(ClientAction.id == action_id,
            ClientAction.task_id == task.id, ClientAction.requester_id == actor.user_id,
            ClientAction.owner_id == actor.user_id, ClientAction.household_id == actor.household_id,
            ClientAction.status == 'PENDING', ClientAction.expires_at > now()))
        if not action:
            return False
        if action.kind == 'cloud.disclose':
            record_ids = []
        elif action.kind in {'authorize_records', 'data.share'}:
            record_ids = payload_of(app, action)['parameters'].get('record_ids') or []
            if not record_ids:
                return False
        else:
            return False
        respond_in_transaction(app, db, actor, action.id, ResponseInput(
            idempotency_key='auto-review:' + action.id, record_ids=record_ids, automatic_review=True))
        return True
    return False
