"""预留用于出站保护，实际用量以已确认账本为准；结算不依赖工具响应解析成功。"""
from sqlalchemy import select


def actual_tokens(response):
    usage = response.get('usage') if isinstance(response, dict) else None
    value = usage.get('total_tokens') if isinstance(usage, dict) else None
    return value if type(value) is int and value >= 0 else None


def reserve_planning(body, key, amount):
    entries = body.setdefault('_planning_usage', {})
    if key in entries:
        raise ValueError('同一规划调用已有预留，不能覆盖')
    entries[key] = {'reserved': amount, 'charged': amount, 'status': 'RESERVED'}
    body['_model_token_charge'] = body.get('_model_token_charge', 0) + amount


def settle_planning(body, key, response):
    entry = body.get('_planning_usage', {}).get(key)
    if not entry:
        return False
    actual = actual_tokens(response)
    charged = entry['charged'] if actual is None else actual
    body['_model_token_charge'] = max(0, body.get('_model_token_charge', 0) - entry['charged'] + charged)
    entry.update(charged=charged, status='ESTIMATED' if actual is None else 'SUCCEEDED')
    return True


def cancel_undispatched_planning(body, key):
    entry = body.get('_planning_usage', {}).get(key)
    if not entry or entry['status'] != 'RESERVED':
        return False
    body['_model_token_charge'] = max(0, body.get('_model_token_charge', 0) - entry['charged'])
    body['_planning_usage'].pop(key)
    return True


UNSETTLED_PLANNING_REASON = '上一次云模型规划未收到可确认的结果（请求失败或执行中断），用量预留仍待核对；不能在原任务中再次发送模型请求。'


def unsettled_cloud_planning(db, actor, task):
    """规划不创建 Invocation；必须单独检查其出站账本，避免重试掩盖原故障。"""
    from .model_routing import ModelUsage
    rows = db.scalars(select(ModelUsage).where(ModelUsage.task_id == task.id,
        ModelUsage.owner_id == actor.user_id, ModelUsage.household_id == actor.household_id,
        ModelUsage.status.in_(['RESERVED', 'UNKNOWN'])))
    return next((row for row in rows if row.call_key.startswith(task.id + ':plan:')), None)


def mark_planning_unknown(body, key):
    """已派发调用的结果不明时只标状态，绝不退还任何已预留用量。"""
    entry = body.get('_planning_usage', {}).get(key)
    if entry and entry.get('status') == 'RESERVED':
        entry['status'] = 'UNKNOWN'


def repair_unsettled_budget_task(app, db, actor, task):
    """仅纠正旧版重试把未决云规划覆盖为任务预算不足的状态，不派发任务。"""
    if task.status != 'WAITING_BUDGET' or task.cancel_requested:
        return False
    body = app.vault.open(task.request, actor.user_id + ':task:' + task.id)
    failure = body.get('_budget_failure', {})
    if not body.get('_agent') or failure.get('code') not in {'task_tokens_exceeded', 'task_cloud_tokens_exceeded'}:
        return False
    unresolved = unsettled_cloud_planning(db, actor, task)
    if unresolved is None:
        return False
    from .data import audit, emit
    body['_superseded_budget_failure'] = body.pop('_budget_failure')
    task.status, task.error = 'FAILED', UNSETTLED_PLANNING_REASON
    task.request = app.vault.seal(body, actor.user_id + ':task:' + task.id)
    audit(db, actor, 'task.planning_failure_restored', task.id, {'call_key': unresolved.call_key})
    emit(db, actor, 'task.updated', task.id)
    return True


def reconcile_cloud_counters(db, actor, task, body):
    """仅在账本完整覆盖调用时校正旧计数；UNKNOWN预留原样保留，不补额度。"""
    from .model_routing import ModelUsage
    rows = list(db.scalars(select(ModelUsage).where(ModelUsage.task_id == task.id,
                    ModelUsage.owner_id == actor.user_id, ModelUsage.household_id == actor.household_id)))
    changes = {}
    if rows and len(rows) == body.get('_cloud_calls'):
        amount = sum(row.charged for row in rows)
        if body.get('_cloud_token_charge', 0) != amount:
            changes['_cloud_token_charge'] = (body.get('_cloud_token_charge', 0), amount)
            body['_cloud_token_charge'] = amount
    prefix = task.id + ':plan:'
    plans = {row.call_key: row for row in rows if row.call_key.startswith(prefix)}
    rounds = body.get('_model_rounds', 0)
    expected = {prefix + str(index) for index in range(rounds)}
    # 旧版本没有本地规划账本；不能用不完整云账本猜测或覆盖本地已用量。
    if rounds and set(plans) == expected and not body.get('_planning_usage'):
        amount = sum(row.charged for row in plans.values())
        if body.get('_model_token_charge', 0) != amount:
            changes['_model_token_charge'] = (body.get('_model_token_charge', 0), amount)
            body['_model_token_charge'] = amount
        body['_planning_usage'] = {key: {'reserved':row.reserved, 'charged':row.charged,
                                       'status':row.status, 'source':'cloud_ledger'} for key,row in plans.items()}
    return changes
