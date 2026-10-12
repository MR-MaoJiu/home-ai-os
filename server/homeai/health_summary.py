"""对已鉴权的健康样本做确定性统计，不将原始健康数据写入记忆。"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta


SLEEP_STAGES = {0: 'in_bed', 1: 'asleep_unspecified', 2: 'awake',
                3: 'asleep_core', 4: 'asleep_deep', 5: 'asleep_rem'}


def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def merged(intervals):
    result = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def duration(intervals):
    return round(sum(end - start for start, end in merged(intervals)), 3)


def sleep_summary(samples, zone):
    """床上时间不当成睡眠；重叠来源取区间并集，矛盾分期单独报告。"""
    events = defaultdict(Counter)
    for item in samples:
        start, end, stage = instant(item['start_at']), instant(item['end_at']), int(item['value'])
        if end <= start:
            continue
        events[start][stage] += 1
        events[end][stage] -= 1
    counts, stages, asleep, awake, in_bed, conflicts = Counter(), defaultdict(list), [], [], [], []
    previous = None
    for current, delta in sorted(events.items()):
        if previous is not None and current > previous:
            span = (previous, current)
            present = {key for key, count in counts.items() if count > 0}
            sleeping = present & {1, 3, 4, 5}
            if 0 in present:
                in_bed.append(span)
            if 2 in present:
                awake.append(span)
            if sleeping:
                asleep.append(span)
                known = sleeping & {3, 4, 5}
                label = 'conflicting_stage' if len(known) > 1 else SLEEP_STAGES[next(iter(known))] if known else 'asleep_unspecified'
                stages[label].append(span)
                if len(known) > 1 or 2 in present:
                    conflicts.append(span)
        counts.update(delta)
        previous = current
    days = defaultdict(float)
    for start, end in merged(asleep):
        cursor = datetime.fromtimestamp(start, zone)
        while cursor.timestamp() < end:
            boundary = (cursor + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            segment_end = min(end, boundary.timestamp())
            days[cursor.date().isoformat()] += segment_end - cursor.timestamp()
            cursor = datetime.fromtimestamp(segment_end, zone)
    return {'asleep_seconds': duration(asleep), 'asleep_hours':round(duration(asleep)/3600,3),
        'in_bed_seconds': duration(in_bed),
        'awake_seconds': duration(awake), 'conflicting_seconds': duration(conflicts),
        'stage_seconds': {name: duration(stages[name]) for name in [
            'asleep_unspecified', 'asleep_core', 'asleep_deep', 'asleep_rem', 'conflicting_stage']},
        'stage_minutes': {name: round(duration(stages[name])/60,3) for name in [
            'asleep_unspecified', 'asleep_core', 'asleep_deep', 'asleep_rem', 'conflicting_stage']},
        'daily': [{'date': day, 'asleep_seconds': round(seconds, 3)} for day, seconds in sorted(days.items())],
        'timezone': str(zone), 'method': 'interval_union',
        'note': '睡眠按实际区间去重，床上时间不计为睡眠；分期重叠冲突单独列出。日期按查询起点时区的自然日切分，不是完整夜晚或医学判断。'}


def summarize(samples, start_at):
    """原始 UUID 重复只统计一次；计量数据按来源分列，避免手机与手表重复相加。"""
    distinct, duplicates = {}, 0
    for item in samples:
        key = (item['type'], item['sample_id'])
        if key in distinct:
            # 相同 UUID 允许分块边界裁剪；不同实测值属于数据冲突，由调用方拒绝。
            duplicates += 1
            old = distinct[key]
            if old != item:
                distinct[key] = {**old,
                    'start_at': min(old['start_at'], item['start_at'], key=instant),
                    'end_at': max(old['end_at'], item['end_at'], key=instant)}
        else:
            distinct[key] = item
    by_type = defaultdict(list)
    for item in distinct.values():
        by_type[item['type']].append(item)
    result = {'sample_count': len(distinct), 'duplicate_count': duplicates, 'metrics': {}}
    zone = datetime.fromisoformat(start_at.replace('Z', '+00:00')).tzinfo
    for kind, items in sorted(by_type.items()):
        sources = defaultdict(list)
        for item in items:
            sources[(item.get('source_bundle', ''), item.get('source_product', ''))].append(item)
        if kind == 'sleep':
            result['metrics'][kind] = {'sample_count': len(items), 'source_count': len(sources), **sleep_summary(items, zone)}
            continue
        source_values = []
        for (bundle, product), entries in sorted(sources.items())[:32]:
            values = [item['value'] for item in entries]
            latest = max(entries, key=lambda item: instant(item['end_at']))
            metric = {'source_bundle': bundle, 'source_product': product,
                'sample_count': len(entries), 'unit': entries[0]['unit'],
                'min': min(values), 'max': max(values), 'sample_mean': round(sum(values) / len(values), 4),
                'latest_value': latest['value'], 'latest_at': latest['end_at']}
            if kind == 'steps':
                metric['recorded_sum'] = sum(values)
            source_values.append(metric)
        result['metrics'][kind] = {'sample_count': len(items), 'sources': source_values,
            'omitted_source_count': max(0, len(sources) - 32),
            'note': '按来源分别统计；样本均值不是时间加权均值，不跨来源相加。来源缺失时可能包含重复计量，不能当作系统健康App的去重总量。'}
    return result


def model_payload(record, payload, limit=4000):
    """给模型提供统计而不是任意截取原样本；小快照也使用相同语义。"""
    from .client_actions import validate_health_record
    from .crypto import canonical
    from fastapi import HTTPException
    parameters = {'types':['sleep'] if record.kind=='health.sleep' else payload.get('types'),
        'start_at':payload.get('start_at'), 'end_at':payload.get('end_at')}
    try:
        validate_health_record(record,payload,parameters)
    except HTTPException:
        return {'health_status':'invalid_snapshot',
            'note':'此健康记录结构或范围无效，不能据此计算；需要按本次问题重新请求对应健康指标。'}
    samples = payload.get('samples') if record.kind=='health.snapshot' else [{
        'type':'sleep','sample_id':record.id,'start_at':payload['start_at'],
        'end_at':payload['end_at'],'value':payload['value'],'unit':'category'}]
    result = {**parameters,'query_complete':payload.get('query_complete'),
        'truncated':payload.get('truncated',False),'summary':summarize(samples,parameters['start_at']),
        'note':'统计来自本条记录内全部已保存样本，原始健康样本仍在服务端数据账本；不代表医学诊断或已获知全部设备记录。'}
    if payload.get('chunk_count',1)>1:
        result.update({key:payload[key] for key in ('capture_id','chunk_index','chunk_count')})
        result['query_complete']=False
        result['note']+=' 此记录只是一次采集的分块，须结合该次采集全部分块的工具汇总，不能把这一块当作整体。'
    if len(canonical(result))<=limit:
        return result
    # 先去掉逐日明细和来源名称，仍保留全量样本的总时长与各分期。
    reduced = {}
    for kind,metric in result['summary']['metrics'].items():
        if kind=='sleep':
            reduced[kind]={key:value for key,value in metric.items() if key not in {'daily','note'}}
        else:
            reduced[kind]={'sample_count':metric['sample_count'],'source_count':len(metric['sources'])+metric['omitted_source_count'],
                'sources':[{key:value for key,value in source.items() if not key.startswith('source_')} for source in metric['sources'][:2]]}
    result['summary']={**result['summary'],'metrics':reduced}
    result['detail_omitted']=True
    if len(canonical(result))<=limit:
        return result
    return {**parameters,'sample_count':len(samples),'summary_omitted':True,
        'note':'当前上下文预算不足，未传入完整统计；请使用健康读取工具按本次需要查询，不能根据样本个数猜测睡眠时长。'}
