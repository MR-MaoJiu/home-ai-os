"""为本次问题选择历史上下文；历史存在不等于本次需要披露。"""
import re
from .privacy import SECRET, PII


PRIVATE_TEXT = re.compile(
    r'健康|病情|病史|诊断|过敏|服药|血压|血糖|心率|步数|睡眠|体重|怀孕|经期|'
    r'精确位置|经纬度|坐标|住址|家庭地址|住在|身份证|银行卡|工资|收入|性取向|私密关系|'
    r'(?i:medical|blood.pressure|heart.rate|diagnosis|precise.location|latitude|longitude)')
FOLLOWUP = re.compile(r'刚才|上一(?:轮|条|个|次)|上次|前面|上面|之前那|这份|那份|'
    r'(?:这个|那个|这些|那些)(?:文件|文档|附件|图片|视频|数据|资料|结果|报告|问题|函数|代码|例子|方案|结论|建议)|'
    r'(?:把|将)(?:它|这个|那个)|^(?:那|所以)?(?:它|这个|那个)(?:是|有|能|怎么|为什么|呢|可以|需要)|'
    r'\b(?:previous|earlier|above|that file|this document)\b', re.I)
CONTINUATION = re.compile(r'^(?:请)?(?:继续|接着|还有呢|然后呢|为什么呢|为什么|什么意思|这是什么意思|那怎么办|那怎么做|那是什么意思|再详细说说|再举个例子|展开说说|说说更多|简短一点|简短点|短一点|详细一点|换成中文|换成英文|用中文说|用英文说)(?:说|讲|分析|解释|总结|处理)?[。！？?！\s]*$')


def private_text(value):
    return bool(SECRET.search(value) or PII.search(value) or PRIVATE_TEXT.search(value))


def linked_versions(old):
    versions = dict(old.get('_record_dependencies', {}))
    for part in [*old.get('_parts', []), *old.get('_references', [])]:
        if part.get('record_id') and type(part.get('version')) is int:
            versions.setdefault(part['record_id'], part['version'])
    for item in old.get('_derived_task_sources', {}).values():
        if isinstance(item, dict) and item.get('source_id') and type(item.get('source_version')) is int:
            versions.setdefault(item['source_id'], item['source_version'])
    return versions


def select_followup(body, entries):
    """明确关联按任务ID；新附件就地处理；普通追问只选择对应最近一轮。"""
    if '_history_followup_turn' in body:
        return next((entry['turn'].id for entry in entries if entry['turn'].id == body['_history_followup_turn']), None)
    if body.get('_reply_to_task_id'):
        return next((entry['turn'].id for entry in entries if entry['task'].id == body['_reply_to_task_id']), None)
    if body.get('record_ids') or body.get('_references') or any(part.get('type') != 'text' for part in body.get('_parts', [])):
        return None
    message = body.get('message', '').strip()
    if not (FOLLOWUP.search(message) or CONTINUATION.fullmatch(message)):
        return None
    # 文件等普通名词本身不是追问；只有明确指向历史时才用来缩小目标。
    group = None
    if re.search(r'文件|文档|附件|图片|视频', message):
        group = 'media'
    elif re.search(r'健康|步数|睡眠|心率|血压|体重', message):
        group = 'health'
    elif re.search(r'位置|坐标|经纬度', message):
        group = 'location'
    if group:
        candidate = next((entry for entry in entries if group in entry['topics']), None)
        return candidate['turn'].id if candidate else None
    return entries[0]['turn'].id if entries else None


def restricted_turn(old, user, answer, sources, *, had_disclosure=False):
    return bool(had_disclosure or old.get('_cloud_consents') or old.get('_history_requires_disclosure')
        or (old.get('_privacy_review') or {}).get('decision') == 'force'
        or private_text(user) or private_text(answer)
        or any(record.sensitivity in {'SECRET', 'SENSITIVE'} or record.cloud_policy == 'LOCAL_ONLY'
               for record in sources.values()))


def plain_request_only(old, user, sources):
    """旧版传染了答案依赖时，仅保留未引用资料的普通用户原话，不复用该答案。"""
    return bool(user and not private_text(user) and not old.get('record_ids')
        and not old.get('_references') and not old.get('_mentions') and not old.get('_task_grants')
        and not old.get('steps') and not old.get('_output_resources') and not old.get('_reply_to_task_id')
        and not old.get('_history_followup_turn') and not FOLLOWUP.search(user) and not CONTINUATION.fullmatch(user.strip())
        and not any(part.get('type') != 'text' for part in old.get('_parts', []))
        and sources)


def health_followup_evidence(app, db, actor, entry, message=''):
    """明确追问才重建健康统计，旧口头答复和旧云批准都不是数据来源。"""
    if entry['pending'] or entry['redacted']:
        return []
    from sqlalchemy import select
    from fastapi import HTTPException
    from .db import Invocation
    from .crypto import canonical
    from .health_reuse import project_outcome
    from .planner import mentioned_health_types
    requested = mentioned_health_types(message)
    readings = []
    remaining = 8000
    rows = db.scalars(select(Invocation).where(Invocation.task_id == entry['task'].id,
        Invocation.owner_id == actor.user_id, Invocation.household_id == actor.household_id,
        Invocation.capability == 'client.request@v1', Invocation.status == 'SUCCEEDED').order_by(Invocation.step))
    for row in rows:
        steps = entry['old'].get('steps', [])
        if row.step >= len(steps) or not row.result:
            continue
        arguments = steps[row.step].get('arguments', {})
        if arguments.get('kind') != 'read_health':
            continue
        receipt = app.vault.open(row.result, actor.user_id + ':invocation-result:' + row.id)
        versions = {item['id']:item['version'] for item in receipt.get('records', [])
            if isinstance(item, dict) and item.get('id') and type(item.get('version')) is int}
        if not versions:
            continue
        sources = entry['sources']
        if any(identifier not in sources or sources[identifier].owner_id != actor.user_id
            or sources[identifier].version != version or entry['versions'].get(identifier) != version
            for identifier, version in versions.items()):
            continue
        try:
            parameters = dict(arguments['parameters'])
            if requested:
                parameters['types'] = [kind for kind in parameters['types'] if kind in requested]
                if not parameters['types']:
                    continue
            outcome = project_outcome(app, [sources[identifier] for identifier in versions], parameters,
                queries=receipt.get('health_queries', []))
        except (HTTPException, KeyError, ValueError, TypeError):
            continue
        value = {'parameters':parameters, 'record_versions':versions, **outcome}
        if len(canonical(value)) > remaining:
            # 收起明细时保留总量和分期，不能用样本个数代替睡眠时长。
            for metric in value['health']['summary']['metrics'].values():
                metric.pop('daily', None)
                if 'sources' in metric:
                    metric['omitted_source_count'] = metric.get('omitted_source_count', 0) + max(0, len(metric['sources']) - 2)
                    metric['sources'] = metric['sources'][:2]
            value['detail_omitted'] = True
        size = len(canonical(value))
        if size <= remaining:
            readings.append(value)
            remaining -= size
    return readings


def health_followup_answerable(message):
    """健康统计已重读时允许直接分析；新增操作仍必须有工具执行回执。"""
    if re.search(r'创建|添加|设置|提醒|自动化|取消|通知|发送|打电话|打开|运行|执行|删除|下载|导出|上传|文件|保存|记住|搜索|联网|重新(?:采|读|获取)|实时|最新', message):
        return False
    return bool(re.search(r'分析|报告|总结|怎么|建议|解释|继续|比较|对比|评估|统计|分期|时长', message))
