"""只针对一次待出站副本复核；任何决定都不能扩大资料授权或跳过硬性边界。"""
import copy
import re
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field
from .contracts import Contract, ProviderManifest
from .crypto import canonical, digest
from .db import Device, Provider, Task, now, uid
from .security import Actor, authenticate, own

router = APIRouter(prefix='/api/v1/tasks', tags=['隐私复核'])


def display_failure(metadata):
    """旧报告只精简已知模板，保留真实检测原因和全部控制字段。"""
    result = dict(metadata)
    if result.get('suggestion') in {
        '可修改待发送文本后重试，或确认仅对本次内容跳过本地模型的隐私判断。',
        '请在管理端检查本地隐私模型配置；也可仅对本次待发送副本作明确决定。',
        '请重新检查当前任务；来源不再授权时需要重新申请。',
    }:
        result['suggestion'] = {
            'privacy_timeout': '请重新检查本次内容。',
            'privacy_not_configured': '请在管理端配置本地隐私模型后重试。',
            'privacy_unavailable': '请检查本地隐私模型服务后重试。',
            'invalid_json': '请检查本地隐私模型的结构化输出后重试。',
            'invalid_schema': '请检查本地隐私模型的输出格式后重试。',
            'invalid_response': '请检查本地隐私模型的响应后重试。',
            'uncertain': '请核对本次待发送内容，可修改后重新检查。',
            'residual_privacy': '请核对本次待发送内容，可修改后重新检查。',
        }.get(result.get('code'), '请重新检查当前任务。')
    # 旧模板只说明服务不可用，不能从中猜测具体是停用还是验证失败。
    if result.get('reason') == '本地隐私模型已停用或尚未通过能力验证':
        result['reason'] = '本地隐私模型当前不可用'
    legacy_reasons = {
        '本地隐私模型无法确定以下内容是否需要脱敏：个人信息（模型未指明类别）': '本地隐私模型无法确定本次内容是否需要脱敏，未返回具体类别。',
        '本地隐私模型无法确认以下内容已充分脱敏：个人信息（模型未指明类别）': '本地隐私模型无法确认本次内容已充分脱敏，未返回具体类别。',
        '本地复核模型认为脱敏后仍可能残留：个人信息（模型未指明类别）': '本地复核模型未确认本次内容已充分脱敏，未返回具体类别。',
    }
    result['reason'] = legacy_reasons.get(result.get('reason'), result.get('reason'))
    return result


def failure_metadata(exc):
    return display_failure({'code': getattr(exc, 'code', 'privacy_unavailable'),
            'stage': getattr(exc, 'stage', 'detect'),
            'reason': str(getattr(exc, 'detail', '隐私检查暂不可用')),
            'suggestion': getattr(exc, 'suggestion', '请检查本地隐私模型后重试。'),
            'retryable': bool(getattr(exc, 'retryable', True)),
            'overrideable': bool(getattr(exc, 'overrideable', False))})


def display_task_error(task, body):
    """聊天与任务卡投影同一条隐私错误，不修改任务持久状态。"""
    if task.status != 'WAITING_PRIVACY' or not task.error:
        return task.error
    metadata = (body or {}).get('_privacy_failure') or {}
    return display_failure({**metadata, 'reason': task.error})['reason']


def authorization_hash(body):
    # 云披露同意单独实时复核，后续补齐同意不能改变此副本的来源绑定。
    binding = {key: body.get(key) or {} for key in (
        '_record_dependencies', '_derived_task_sources', '_task_grants', '_client_context')}
    binding.update({key: body.get(key) or [] for key in ('record_ids', '_history_snapshot', '_parts')})
    binding.update(device_id=body.get('device_id'), _automation_scope=body.get('_automation_scope'),
                   _client_context_expires_at=body.get('_client_context_expires_at') or 0)
    return digest(canonical(binding))


def _blocks(arguments):
    blocks = []
    roles = {'system': '系统规则', 'user': '用户与已授权资料', 'assistant': '已有回答', 'tool': '工具返回资料'}
    for index, message in enumerate(arguments.get('messages', [])):
        role = message.get('role', '')
        label = roles.get(role, '消息') + ' ' + str(index + 1)
        content = message.get('content')
        if isinstance(content, str):
            blocks.append({'id': f'm{index}', 'label': label, 'text': content, 'editable': role in {'user', 'assistant', 'tool'}})
        elif isinstance(content, list):
            for part_index, part in enumerate(content):
                if part.get('type') == 'text' and isinstance(part.get('text'), str):
                    blocks.append({'id': f'm{index}p{part_index}', 'label': label, 'text': part['text'], 'editable': role in {'user', 'assistant', 'tool'}})
                elif part.get('type') == 'image_url':
                    blocks.append({'id': f'm{index}p{part_index}', 'label': '附件图像', 'text': '图像像素不会通过文字修改脱敏，仍需要资料本人批准云端披露。', 'editable': False})
        for call_index, call in enumerate(message.get('tool_calls') or []):
            function = call.get('function', {})
            blocks.append({'id': f'm{index}c{call_index}', 'label': '已确定的工具参数',
                           'text': str(function.get('name', '')) + '\n' + str(function.get('arguments', '')), 'editable': False})
    return blocks


def _device_valid(db, actor, body):
    device = db.get(Device, body.get('device_id'))
    if not device or device.revoked or device.user_id != actor.user_id:
        raise HTTPException(409, '原任务设备已撤销，不能继续发送')


def validate_review(app, db, actor, task, body, review, *, allow_expired=False):
    from .model_routing import fingerprint
    from .result_access import check_dependencies
    from .client_actions import read_authorized_record
    if task.cancel_requested or task.status == 'CANCELED':
        raise HTTPException(409, '任务已取消，不能继续发送')
    if task.status in {'FAILED', 'SUCCEEDED'}:
        raise HTTPException(409, '任务已结束，不能继续发送')
    _device_valid(db, actor, body)
    if review.get('approved_device_id'):
        approved_device = db.get(Device, review['approved_device_id'])
        if not approved_device or approved_device.revoked or approved_device.user_id != actor.user_id:
            raise HTTPException(409, '批准此次复核的设备已撤销，请重新检查')
    check_dependencies(db, actor, body)
    if review['request_hash'] != task.request_hash:
        raise HTTPException(409, '原任务内容已变化，请重新检查')
    if review['authorization_hash'] != authorization_hash(body):
        raise HTTPException(409, '资料授权范围已变化，请重新检查')
    provider = db.get(Provider, review['provider']['id'])
    if not provider:
        raise HTTPException(409, '目标模型已不存在，请重新检查')
    if not provider.enabled:
        raise HTTPException(409, '目标模型已停用，请重新检查')
    if fingerprint(ProviderManifest.model_validate_json(provider.manifest)) != review['provider_fingerprint']:
        raise HTTPException(409, '目标模型配置已变化，请重新检查')
    for identifier, version in review['versions'].items():
        record = read_authorized_record(db, actor, identifier, body)
        if record.version != version:
            raise HTTPException(409, '来源资料版本已变化，请重新检查')
        if record.sensitivity == 'SECRET':
            raise HTTPException(409, '来源资料已标记为秘密，不能发送')
    if not allow_expired and review['expires_at'] <= now():
        raise HTTPException(409, '此次隐私复核已过期，请重新检查')


def capture_review(app, db, actor, task, body, manifest, original, call_key, exc, *, candidate, mapping, detected, versions):
    """正文和占位符映射只存于任务的信封加密载荷，不进入普通日志。"""
    from .model_routing import fingerprint
    from .cloud_gateway import inspection_messages
    metadata = failure_metadata(exc)
    body['_privacy_failure'] = metadata
    if not exc.overrideable:
        return None
    _, images = inspection_messages(candidate['messages'])
    expires_at = now() + 86400
    if body.get('_client_context') and body.get('_client_context_expires_at', 0) > now():
        expires_at = min(expires_at, body['_client_context_expires_at'])
    review = {**metadata, 'id': uid(), 'task_id': task.id, 'status': 'pending',
              'created_at': now(), 'expires_at': expires_at, 'call_key': call_key,
              'request_hash': task.request_hash, 'arguments_hash': digest(canonical(original)),
              'authorization_hash': authorization_hash(body), 'versions': dict(versions),
              'provider_fingerprint': fingerprint(manifest),
              'provider': {'id': manifest.id, 'model': manifest.model, 'endpoint': manifest.endpoint},
              'original': copy.deepcopy(original), 'candidate': copy.deepcopy(candidate),
              'mapping': dict(mapping), 'detected': copy.deepcopy(detected), 'has_media': images > 0}
    body['_privacy_review'] = review
    task.request = app.vault.seal(body, actor.user_id + ':task:' + task.id)
    exc.review = {'review_id': review['id'], 'expires_at': review['expires_at']}
    return review


def approved_review(app, db, actor, task, body, manifest, original, call_key):
    review = body.get('_privacy_review')
    if not review or review.get('status') == 'consumed':
        return None
    validate_review(app, db, actor, task, body, review)
    from .model_routing import fingerprint
    if review['call_key'] != call_key or review['arguments_hash'] != digest(canonical(original)) or review['provider_fingerprint'] != fingerprint(manifest):
        raise HTTPException(409, '待发送内容已变化，旧复核不能用于新请求')
    if review['status'] != 'approved':
        from .cloud_gateway import PrivacyUnavailable
        raise PrivacyUnavailable(review['reason'], code=review['code'], stage=review['stage'],
                                 suggestion=review['suggestion'], retryable=review['retryable'],
                                 overrideable=review['overrideable'], review={'review_id': review['id']})
    if review.get('approved_candidate_hash') != digest(canonical(review['candidate'])):
        raise HTTPException(409, '复核副本摘要不一致，禁止发送')
    return review


def consume_review(app, db, actor, task, body, review, outgoing):
    """与预算预留、披露记录一起提交；发送失败也不能再次消费这一批准。"""
    if not review:
        return
    db.refresh(task, with_for_update=True)
    latest = app.vault.open(task.request, actor.user_id + ':task:' + task.id)
    current = latest.get('_privacy_review')
    if not current or current['id'] != review['id'] or current['status'] != 'approved':
        raise HTTPException(409, '此复核已被使用或替换，禁止重复发送')
    validate_review(app, db, actor, task, latest, current)
    if current.get('approved_candidate_hash') != review.get('approved_candidate_hash'):
        raise HTTPException(409, '复核副本已变化，禁止发送')
    if review['decision'] == 'force' and digest(canonical(outgoing)) != review['approved_candidate_hash']:
        raise HTTPException(409, '实际出站内容不同于此次批准的副本，禁止强制发送')
    current.update({'status': 'consumed', 'consumed_at': now(), 'outgoing_hash': digest(canonical(outgoing))})
    for key in ('original', 'candidate', 'mapping', 'detected'):
        current.pop(key, None)
    body['_privacy_review'] = current
    body.pop('_privacy_failure', None)
    from .data import audit
    audit(db, actor, 'privacy.explicit_override' if review['decision'] == 'force' else 'privacy.review_consumed', task.id,
          {'review_id': review['id'], 'decision': review['decision'], 'provider_id': review['provider']['id'], 'outgoing_hash': current['outgoing_hash']})


class ReviewDecision(Contract):
    review_id: str = Field(min_length=1, max_length=100)
    decision: Literal['retry', 'force', 'edit']
    edits: dict[str, str] = Field(default_factory=dict, max_length=100)
    idempotency_key: str = Field(min_length=8, max_length=200)


def _signed_device(db, actor):
    # 浏览器管理身份不能代替用户设备批准私人内容。
    device = db.get(Device, actor.device_id)
    if not device or device.revoked or device.user_id != actor.user_id:
        raise HTTPException(403, '请使用本人已配对设备处理隐私复核')


@router.get('/{task_id}/privacy-review')
def read_review(task_id: str, request: Request, actor: Actor = Depends(authenticate)):
    app = request.app.state
    with app.db() as db:
        task = own(db, Task, task_id, actor)
        _signed_device(db, actor)
        db.refresh(task, with_for_update=True)
        body = app.vault.open(task.request, actor.user_id + ':task:' + task.id)
        if purge_expired_review(body):
            task.request = app.vault.seal(body, actor.user_id + ':task:' + task.id)
            db.commit()
        review = body.get('_privacy_review')
        if task.status == 'WAITING_PRIVACY' and review and review.get('status') not in {'pending', 'expired'}:
            # 后续分类或搜索复核失败没有可批准的云副本，不能回显上一轮已消费的挑战。
            review = None
        if not review:
            if task.status != 'WAITING_PRIVACY' and not body.get('_privacy_failure'):
                raise HTTPException(404, '当前任务没有待处理的隐私复核')
            metadata = body.get('_privacy_failure') or {'code': 'privacy_unavailable', 'stage': 'unknown', 'reason': '旧任务未记录详细原因，重新检查可生成报告。', 'suggestion': '请点击重新检查以取得当前检测结果。', 'retryable': True}
            return {**display_failure(metadata), 'overrideable': False, 'review_id': None, 'task_id': task.id, 'status': 'unavailable',
                    'expires_at': None, 'provider': None, 'blocks': [], 'has_media': False, 'can_force': False, 'can_edit': False}
        result = display_failure({key: review[key] for key in ('code', 'stage', 'reason', 'suggestion', 'retryable', 'overrideable', 'expires_at', 'provider', 'has_media')})
        status, valid = review['status'], True
        if status != 'consumed':
            try:
                validate_review(app, db, actor, task, body, review, allow_expired=True)
            except HTTPException as exc:
                status, valid = 'invalidated', False
                result.update({'reason': '此复核已失效：' + str(exc.detail), 'suggestion': '请重新检查当前任务。'})
                # 失效内容不仅停止展示，也从加密复核副本移除，避免留下过期授权的重复副本。
                review['status'] = 'invalidated'
                for key in ('original', 'candidate', 'mapping', 'detected'):
                    review.pop(key, None)
                body['_privacy_failure'] = {'code': 'context_changed', 'stage': 'authorization',
                    'reason': result['reason'], 'suggestion': result['suggestion'], 'retryable': True, 'overrideable': False}
                task.request = app.vault.seal(body, actor.user_id + ':task:' + task.id)
                db.commit()
        if valid and status != 'consumed' and review['expires_at'] <= now():
            status = 'expired'
        pending = valid and status == 'pending' and task.status == 'WAITING_PRIVACY'
        blocks = _blocks(review['candidate']) if valid and review.get('candidate') else []
        return {**result, 'review_id': None if status in {'expired', 'invalidated'} else review['id'], 'task_id': task.id, 'status': status, 'blocks': blocks,
                'can_force': pending and review['overrideable'], 'can_edit': pending and any(block['editable'] for block in blocks)}


@router.post('/{task_id}/privacy-review')
def decide_review(task_id: str, decision: ReviewDecision, request: Request, actor: Actor = Depends(authenticate)):
    app = request.app.state
    with app.db() as db:
        result = decide_in_transaction(app, db, actor, task_id, decision)
        db.commit()
        return result


def decide_in_transaction(app, db, actor, task_id, decision, *, automatic=False):
    from .data import audit, emit
    from .privacy import ensure_model_safe
    task = own(db, Task, task_id, actor)
    _signed_device(db, actor)
    db.refresh(task, with_for_update=True)
    body = app.vault.open(task.request, actor.user_id + ':task:' + task.id)
    key_hash, request_hash = digest(decision.idempotency_key.encode()), digest(canonical(decision.model_dump()))
    submissions = body.get('_privacy_review_submissions', [])
    prior = next((entry for entry in submissions if entry['key_hash'] == key_hash), None)
    if prior:
        if prior['request_hash'] != request_hash:
            raise HTTPException(409, '幂等键已用于不同复核决定')
        return {**prior['result'], 'replayed': True}
    review = body.get('_privacy_review')
    if not review or review['id'] != decision.review_id or review['status'] != 'pending':
        raise HTTPException(409, '隐私复核已被替换或处理，请刷新')
    if task.status != 'WAITING_PRIVACY':
        raise HTTPException(409, '任务当前不在隐私等待状态')
    validate_review(app, db, actor, task, body, review)
    if decision.decision == 'force' and not review['overrideable']:
        raise HTTPException(403, '此类检查不能强制通过')
    if decision.decision != 'edit' and decision.edits:
        raise HTTPException(422, '只有修改后重试可以提交编辑内容')
    if decision.decision == 'edit':
        if not decision.edits:
            raise HTTPException(422, '请至少修改一个可编辑文本块')
        allowed = {block['id'] for block in _blocks(review['candidate']) if block['editable']}
        if set(decision.edits) - allowed:
            raise HTTPException(422, '不能修改系统规则、工具参数或媒体')
        for identifier, value in decision.edits.items():
            if len(value.encode()) > 100000:
                raise HTTPException(422, '修改后的文本过大')
            ensure_model_safe(value)
            if any(token not in review.get('mapping', {}) for token in re.findall(r'<PRIVATE_[a-f0-9]{12}_\d+>', value)):
                raise HTTPException(422, '不能添加未显示的私人信息占位符')
            coordinates = identifier[1:].split('p')
            message = review['candidate']['messages'][int(coordinates[0])]
            if len(coordinates) == 1:
                message['content'] = value
            else:
                message['content'][int(coordinates[1])]['text'] = value
        from .cloud_gateway import inspection_messages
        if len(canonical(inspection_messages(review['candidate']['messages'])[0])) > 100000:
            raise HTTPException(422, '修改后的总文本过大')
    review.update({'status': 'approved', 'decision': decision.decision,
                   'approved_candidate_hash': digest(canonical(review['candidate'])), 'approved_at': now(),
                   'approved_device_id': actor.device_id})
    task.deadline = now() + min(body.get('timeout_seconds', 600), 3600)
    task.status, task.error = 'RECEIVED', None
    result = {'review_id': review['id'], 'status': 'RECEIVED', 'decision': decision.decision, 'replayed': False}
    body['_privacy_review_submissions'] = (submissions + [{'key_hash': key_hash, 'request_hash': request_hash, 'result': result}])[-32:]
    task.request = app.vault.seal(body, actor.user_id + ':task:' + task.id)
    audit(db, actor, 'privacy.review_decision', task.id, {'review_id': review['id'], 'decision': decision.decision, 'provider_id': review['provider']['id'], **({'automatic':True,'device_id':actor.device_id} if automatic else {})})
    emit(db, actor, 'task.updated', task.id)
    return result


def reset_for_resume(body, status):
    review = body.get('_privacy_review')
    if review and (status == 'WAITING_PRIVACY' or review.get('expires_at', 0) <= now()):
        body.pop('_privacy_review', None)
    body.pop('_privacy_failure', None)


def purge_expired_review(body, timestamp=None):
    """清除过期副本正文，保留无正文的原因、摘要和幂等回执。"""
    review = body.get('_privacy_review')
    if not review or review.get('expires_at', 0) > (now() if timestamp is None else timestamp):
        return False
    changed = any(key in review for key in ('original', 'candidate', 'mapping', 'detected'))
    for key in ('original', 'candidate', 'mapping', 'detected'):
        review.pop(key, None)
    if review.get('status') not in {'consumed', 'expired'}:
        review['status'] = 'expired'
        changed = True
    return changed
