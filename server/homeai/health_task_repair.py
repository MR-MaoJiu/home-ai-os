"""仅供本机维护：恢复旧版在健康参数校验阶段终止且确实没有派发的任务。"""
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .client_actions import ClientAction, _instant
from .crypto import canonical, digest
from .data import audit, emit
from .db import Device, Invocation, Task, now
from .result_access import check_dependencies
from .security import own


def requeue_invalid_health_window(app, db, actor, task_id):
    """调用方持有任务咨询锁并提交；不恢复授权、不修改日期、不重置费用。"""
    task = own(db, Task, task_id, actor)
    db.refresh(task, with_for_update=True)
    if task.status != 'FAILED' or task.cancel_requested or task.result or now() - task.created_at > 86400:
        return False
    if task.error != '健康数据时间范围无效或超过31天':
        return False
    body = app.vault.open(task.request, actor.user_id + ':task:' + task.id)
    if not body.get('_agent') or len(body.get('steps', [])) != 1 or any(body.get(key) for key in
            ('_privacy_review', '_task_grants', '_client_wait', '_health_window_repair', '_android_task_id')):
        return False
    device = db.get(Device, body.get('device_id'))
    if not device or device.revoked or device.user_id != actor.user_id:
        return False
    check_dependencies(db, actor, body)
    from .token_accounting import unsettled_cloud_planning
    if unsettled_cloud_planning(db, actor, task) is not None:
        return False
    rows = list(db.scalars(select(Invocation).where(Invocation.task_id == task.id)))
    if len(rows) != 1:
        return False
    row = rows[0]
    if row.owner_id != actor.user_id or row.household_id != actor.household_id or row.step != 0 or row.status != 'FAILED' or row.capability != 'client.request@v1' or row.result:
        return False
    # 请求创建的全部写入共用事务。已有任意待办，即使已拒绝或取消，也不能重开。
    if db.scalar(select(ClientAction.id).where(ClientAction.task_id == task.id).limit(1)):
        return False
    arguments = app.vault.open(row.arguments, actor.user_id + ':invocation:' + row.id)
    if body['steps'][0] != {'capability': row.capability, 'arguments': arguments} or row.arguments_hash != digest(canonical(arguments)):
        return False
    if arguments.get('kind') != 'read_health' or arguments.get('target_member_id') not in {'self', actor.user_id}:
        return False
    parameters = arguments.get('parameters')
    if not isinstance(parameters, dict) or set(parameters) != {'types', 'start_at', 'end_at'}:
        return False
    try:
        start, end = _instant(parameters['start_at']), _instant(parameters['end_at'])
    except (ValueError, TypeError, OverflowError):
        return False
    if not 0 < end - start <= 31 * 86400 or end <= task.created_at + 300:
        return False
    # 保留原错误计划及调用账本，只登记未执行；新的计划仍从 Core 鉴权与校验开始。
    body['_health_window_repair'] = {'at': now(), 'invocation_id': row.id, 'previous_error': task.error}
    reference = datetime.fromtimestamp(task.created_at, ZoneInfo(body.get('timezone', 'Asia/Shanghai'))).isoformat(timespec='seconds')
    body['_health_plan_feedback'] = ('原健康读取计划因结束时间在未来而未执行，尚未向手机申请读取。'
        '本任务接收时间为' + reference + '，请按原请求修正时间，结束不晚于接收时间；今天不能填写今晚23:59:59。'
        '保留原指标和范围意图，不得虚称已有健康样本。')
    row.status = 'SKIPPED'
    row.result = app.vault.seal({'status': 'not_executed', 'reason': 'invalid_health_window',
        'code': 'health_time_future_end', 'not_dispatched': True}, actor.user_id + ':invocation-result:' + row.id)
    task.status, task.error = 'RECEIVED', None
    task.deadline = now() + min(body.get('timeout_seconds', 600), 3600)
    task.request = app.vault.seal(body, actor.user_id + ':task:' + task.id)
    audit(db, actor, 'health.failed_plan_requeued', task.id, {'not_dispatched': True, 'invocation_id': row.id})
    emit(db, actor, 'task.updated', task.id)
    return True
