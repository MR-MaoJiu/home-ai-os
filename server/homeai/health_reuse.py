"""健康数据先复用本人账本；投影不扩大采集或成员授权范围。"""
from collections import defaultdict
from datetime import datetime, timezone
import re

from fastapi import HTTPException
from sqlalchemy import select

from .db import Record


def _groups(app, records):
    groups = {}
    for record in records:
        payload = app.vault.open(record.payload, record.owner_id + ':record:' + record.id)
        if record.kind not in {'health.snapshot', 'health.sleep'}:
            continue
        capture = payload.get('capture_id')
        key = (record.owner_id, capture) if isinstance(capture, str) else ('record', record.id)
        groups.setdefault(key, []).append((record, payload))
    return list(groups.values())


def project_outcome(app, records, parameters, *, queries=()):
    """先验证原始快照，再投影所需指标和实际重叠范围；不猜测区间外数据。"""
    from .client_actions import _instant, health_outcome
    from .health_summary import merged, summarize
    requested = parameters['types']
    lower, upper = _instant(parameters['start_at']), _instant(parameters['end_at'])
    ranges = defaultdict(list)
    complete_ranges = defaultdict(list)
    queried, samples = set(), []
    incomplete, boundary_quantities = False, False
    values = {}
    for group in _groups(app, records):
        first = group[0][1]
        original = {'types': first.get('types', ['sleep']),
                    'start_at': first['start_at'], 'end_at': first['end_at']}
        original_outcome = health_outcome(app, [row for row, _ in group], original)
        start, end = max(lower, _instant(first['start_at'])), min(upper, _instant(first['end_at']))
        kinds = set(original['types']) & set(requested)
        if end <= start or not kinds:
            continue
        queried.update(kinds)
        for kind in kinds:
            ranges[kind].append((start, end))
        health = original_outcome['health']
        if health['query_complete'] is True and health['time_range_complete']:
            for kind in kinds:
                complete_ranges[kind].append((start, end))
        elif health['query_complete'] is False:
            incomplete = True
        for row, payload in group:
            raw = payload.get('samples') if row.kind == 'health.snapshot' else [{
                'type': 'sleep', 'sample_id': row.id, 'start_at': payload['start_at'],
                'end_at': payload['end_at'], 'value': payload['value'], 'unit': 'category'}]
            for item in raw:
                if item['type'] not in kinds:
                    continue
                begin, finish = _instant(item['start_at']), _instant(item['end_at'])
                if finish < lower or begin > upper or (finish == lower and begin < finish) or (begin == upper and begin < finish):
                    continue
                if item['type'] == 'sleep':
                    if min(finish, upper) <= max(begin, lower):
                        continue
                    item = {**item,
                        'start_at': datetime.fromtimestamp(max(begin, lower), timezone.utc).isoformat(),
                        'end_at': datetime.fromtimestamp(min(finish, upper), timezone.utc).isoformat()}
                elif begin < lower or finish > upper:
                    # 步数、能量等总量不能按时间比例分摊，也不能包含窗口外的值。
                    boundary_quantities = True
                    continue
                key = (item['type'], item['sample_id'])
                old = values.get(key)
                if old and (old['value'] != item['value'] or old['unit'] != item['unit']):
                    raise HTTPException(409, '已保存的同一健康样本存在不同值，请核对来源')
                values[key] = item
                samples.append(item)
    # 只有 Core 验证过的采集回执可到达这里；空结果仍表明该范围已经查过。
    for query in queries:
        kinds = set(query['types']) & set(requested)
        start, end = max(lower, _instant(query['start_at'])), min(upper, _instant(query['end_at']))
        if end <= start:
            continue
        queried.update(kinds)
        for kind in kinds:
            if query.get('time_range_complete'):
                ranges[kind].append((start, end))
            if query.get('query_complete') is True and query.get('time_range_complete'):
                complete_ranges[kind].append((start, end))
        if query.get('query_complete') is False:
            incomplete = True
    def full(intervals):
        return any(start <= lower and end >= upper for start, end in merged(intervals))
    coverage = {kind: {'time_range_complete': full(ranges[kind]),
        'query_complete': full(complete_ranges[kind]),
        'provided_ranges': [{'start_at': datetime.fromtimestamp(start, timezone.utc).isoformat(),
            'end_at': datetime.fromtimestamp(end, timezone.utc).isoformat()} for start, end in merged(ranges[kind])]}
        for kind in requested}
    available = {sample['type'] for sample in samples}
    summary = summarize(samples, parameters['start_at'])
    time_complete = all(item['time_range_complete'] for item in coverage.values()) and not incomplete
    query_complete = all(item['query_complete'] for item in coverage.values())
    status = 'no_data' if not samples else ('available' if time_complete and available == set(requested) else 'partial')
    note = '已使用家庭服务器中保存的健康数据，不需要再次提供同一份资料；统计仅覆盖实际已提供的指标和时间。'
    if not time_complete:
        note += ' 请求范围没有完全覆盖，只能分析已有部分，不能声称取得全部期间或当前实时数据。'
    if set(requested) - available:
        note += ' 没有样本的指标不代表零值或拒绝了系统权限。'
    if not query_complete:
        note += ' 有些来源未记录完整采集状态或未覆盖全部范围，不能保证已读到设备上的全部记录。'
    if boundary_quantities:
        note += ' 跨查询边界的计量样本未按比例估算，相关统计仅包含完整落在范围内的样本。'
    return {'data_status': status, 'note': note, 'health': {
        'requested_types': requested, 'available_types': [kind for kind in requested if kind in available],
        'missing_types': [kind for kind in requested if kind not in available],
        'queried_types': [kind for kind in requested if kind in queried],
        'uncollected_types': [kind for kind in requested if kind not in queried],
        'sample_count': summary['sample_count'], 'summary': summary,
        'requested_start_at': parameters['start_at'], 'requested_end_at': parameters['end_at'],
        'time_range_complete': time_complete, 'query_complete': True if query_complete else (False if incomplete else None),
        'coverage_by_type': coverage,
        'provided_ranges': [{'start_at': datetime.fromtimestamp(start, timezone.utc).isoformat(),
            'end_at': datetime.fromtimestamp(end, timezone.utc).isoformat()}
            for start, end in merged([span for spans in ranges.values() for span in spans])],
    }}


def select_saved(app, db, actor, body, parameters, target):
    """只检索本人历史账本或当前任务明确授权的来源，不把他人授权带入新任务。"""
    from .client_actions import _instant, read_authorized_record
    from .data import accessible
    explicit = set(body.get('record_ids', [])) | set(body.get('_historical_record_ids', [])) | set(body.get('_task_grants', {}))
    rows = {}
    for identifier in explicit:
        row = read_authorized_record(db, actor, identifier, body)
        if row.owner_id == target and row.kind in {'health.snapshot', 'health.sleep'} and row.sensitivity != 'SECRET':
            expected = body.get('_record_dependencies', {}).get(identifier)
            if expected is not None and expected != row.version:
                raise HTTPException(409, '健康资料版本已变化，需要重新查询')
            rows[row.id] = row
    if target == actor.user_id:
        for row in db.scalars(accessible(db, actor).where(Record.owner_id == actor.user_id,
                Record.kind == 'health.snapshot', Record.sensitivity != 'SECRET').order_by(Record.updated_at.desc()).limit(200)):
            rows.setdefault(row.id, row)
    lower, upper = _instant(parameters['start_at']), _instant(parameters['end_at'])
    candidates = []
    for group in _groups(app, list(rows.values())):
        payload = group[0][1]
        try:
            overlap = min(upper, _instant(payload['end_at'])) - max(lower, _instant(payload['start_at']))
            kinds = set(payload.get('types', ['sleep'])) & set(parameters['types'])
            if overlap <= 0 or not kinds:
                continue
            outcome = project_outcome(app, [row for row, _ in group], parameters)
            if outcome['health']['query_complete'] is False and not any(row.id in explicit for row, _ in group):
                continue
            # 空采集可在原任务中抑制重复；新任务仍允许用户重新采集。
            if not outcome['health']['sample_count'] and not any(row.id in explicit for row, _ in group):
                continue
            candidates.append((len(kinds), overlap, group, outcome))
        except (HTTPException, KeyError, TypeError, ValueError):
            if any(row.id in explicit for row, _ in group):
                raise
    candidates.sort(key=lambda item: (all(payload.get('query_complete') is True for _, payload in item[2]),
        max(row.updated_at for row, _ in item[2]), item[0], item[1]), reverse=True)
    selected, coverage = [], defaultdict(list)
    from .health_summary import merged
    for _, _, group, outcome in candidates:
        if len(selected) + len(group) > 20:
            continue
        additions = {kind: [(_instant(span['start_at']), _instant(span['end_at'])) for span in details['provided_ranges']]
            for kind, details in outcome['health']['coverage_by_type'].items() if details['provided_ranges']}
        if all(merged(coverage[kind] + spans) == merged(coverage[kind]) for kind, spans in additions.items()):
            continue
        selected.extend(row for row, _ in group)
        for kind, spans in additions.items():
            coverage[kind].extend(spans)
    return (selected, project_outcome(app, selected, parameters)) if selected else None


def refresh_requested(body):
    """只有明确重采或实时指标才绕过历史资料；普通“现在帮我分析”不算。"""
    return bool(re.search(r'(?:重新|再次|重)[采读取测]{1,3}|实时(?:健康|心率|步数|血氧|数据)|'
        r'(?:现在|当前|最新)的?(?:心率|血氧|体温|步数|健康数据)|(?:心率|血氧|体温|步数)的?(?:实时|最新)',
        body.get('message', '')))


def inventory(app, db, actor, body, types=None):
    """给规划器的数据目录只有范围和版本；不含样本值，也不是云端披露授权。"""
    from .data import accessible
    from .client_actions import health_outcome
    rows = list(db.scalars(accessible(db, actor).where(Record.owner_id == actor.user_id,
        Record.kind == 'health.snapshot', Record.sensitivity != 'SECRET').order_by(Record.updated_at.desc()).limit(200)))
    items, count = [], 0
    for group in _groups(app, rows):
        payload = group[0][1]
        kinds = [kind for kind in payload.get('types', []) if types is None or kind in types]
        if not kinds or count + len(group) > 20:
            continue
        try:
            original = {key: payload[key] for key in ('types', 'start_at', 'end_at')}
            result = health_outcome(app, [row for row, _ in group], original)
        except (HTTPException, KeyError, TypeError, ValueError):
            continue
        if not result['health']['sample_count']:
            continue
        items.append({'types': kinds, 'start_at': payload['start_at'], 'end_at': payload['end_at'],
            'query_complete': result['health']['query_complete'],
            'record_versions': {row.id: row.version for row, _ in group}})
        count += len(group)
        if len(items) >= 8:
            break
    return items


def narrow_pending_request(app, db, actor, task_id):
    """本机维护：只替换尚未响应的重复采集卡片，不执行模型或恢复旧审批。"""
    from .client_actions import (ClientAction, WAITING_STATUS, _response_notice, execute,
        payload_of)
    from .crypto import canonical, digest
    from .data import audit, emit
    from .db import Device, Invocation, Task, now
    from .result_access import check_dependencies, capture_result_dependencies
    from .security import own
    task = own(db, Task, task_id, actor)
    db.refresh(task, with_for_update=True)
    if task.status != WAITING_STATUS or task.cancel_requested:
        return {'changed': False}
    body = app.vault.open(task.request, actor.user_id + ':task:' + task.id)
    if refresh_requested(body):
        return {'changed': False}
    wait = body.get('_client_wait', {})
    action = db.scalar(select(ClientAction).where(ClientAction.id == wait.get('action_id'),
        ClientAction.task_id == task.id, ClientAction.requester_id == actor.user_id,
        ClientAction.owner_id == actor.user_id).with_for_update().execution_options(populate_existing=True))
    if not action or action.kind != 'read_health' or action.status != 'PENDING' or action.response or action.response_key or action.expires_at <= now():
        return {'changed': False}
    invocation = db.get(Invocation, action.invocation_id)
    device = db.get(Device, body.get('device_id'))
    if not invocation or invocation.task_id != task.id or invocation.status != WAITING_STATUS or invocation.result or not device or device.revoked or device.user_id != actor.user_id:
        return {'changed': False}
    arguments = app.vault.open(invocation.arguments, actor.user_id + ':invocation:' + invocation.id)
    if (invocation.capability != 'client.request@v1' or arguments.get('kind') != 'read_health'
        or arguments.get('target_member_id') not in {'self', actor.user_id}
        or invocation.arguments_hash != digest(canonical(arguments))
        or payload_of(app, action)['parameters'] != arguments.get('parameters')):
        return {'changed': False}
    if (invocation.step >= len(body.get('steps', []))
        or body['steps'][invocation.step] != {'capability':invocation.capability,'arguments':arguments}):
        return {'changed': False}
    check_dependencies(db, actor, body)
    selected = select_saved(app, db, actor, body, arguments['parameters'], actor.user_id)
    if not selected:
        return {'changed': False}
    records, outcome = selected
    missing = outcome['health']['uncollected_types']
    if len(missing) >= len(arguments['parameters']['types']):
        return {'changed': False}
    action.status = 'CANCELED'
    _response_notice(db, actor, action)
    body.pop('_client_wait', None)
    task.status, task.deadline = 'RECEIVED', now() + max(1, min(3600, wait.get('remaining_seconds', 600)))
    task.request = app.vault.seal(body, actor.user_id + ':task:' + task.id)
    invocation.status = 'PENDING'
    result = execute(app, db, actor, task, invocation, invocation.capability, arguments,
        request_key='health-reuse-v1:' + action.id)
    if result.get('status') == 'waiting_client':
        replacement = db.get(ClientAction, result['client_action_id'])
        replacement.expires_at = action.expires_at
        task.deadline = action.expires_at
    else:
        capture_result_dependencies(db, actor, body, invocation.capability, result)
        task.request = app.vault.seal(body, actor.user_id + ':task:' + task.id)
        invocation.status = 'SUCCEEDED'
        invocation.result = app.vault.seal(result, actor.user_id + ':invocation-result:' + invocation.id)
    audit(db, actor, 'health.pending_request_narrowed', task.id, {
        'previous_action_id': action.id, 'client_action_id': result.get('client_action_id'),
        'reused_records': len(records), 'requested_types': missing, 'model_not_called': True})
    emit(db, actor, 'task.updated', task.id)
    return {'changed': True, 'previous_action_id': action.id, 'client_action_id': result.get('client_action_id'),
        'requested_types': missing, 'reused_records': len(records), 'model_not_called': True}
