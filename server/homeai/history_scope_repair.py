"""有界修复旧版历史污染待办；由维护调用者持任务咨询锁并提交事务。"""
import copy
import math

from fastapi import HTTPException
from sqlalchemy import select

from .client_actions import ClientAction, payload_of, _response_notice
from .conversations import TERMINAL, history
from .crypto import digest
from .data import audit, emit
from .db import Conversation, ConversationTurn, Disclosure, Invocation, Task, now, scope
from .history_scope import linked_versions, private_text
from .model_routing import ModelUsage


def _versions(value):
    return isinstance(value, dict) and all(isinstance(key, str) and type(version) is int
                                         and version > 0 for key, version in value.items())


def _historical_proof(app, db, actor, task, body):
    """仅相信本成员、本会话和原快照中仍匹配的来源，不能借其他任务证明。"""
    snapshot = body.get('_history_snapshot')
    if not isinstance(snapshot, list) or not 1 <= len(snapshot) <= 200:
        return None
    versions, derived = {}, {}
    for entry in snapshot:
        if not isinstance(entry, dict) or not isinstance(entry.get('id'), str):
            return None
        turn = db.get(ConversationTurn, entry['id'])
        if (not turn or turn.owner_id != actor.user_id or turn.household_id != actor.household_id
                or turn.conversation_id != body['_conversation_id']
                or turn.sequence >= body['_conversation_sequence']):
            return None
        old_task = db.get(Task, turn.task_id)
        if (not old_task or old_task.id == task.id or old_task.owner_id != actor.user_id
                or old_task.household_id != actor.household_id or old_task.status not in TERMINAL
                or digest((old_task.result or '').encode()) != entry.get('result_hash')):
            return None
        old = app.vault.open(old_task.request, actor.user_id + ':task:' + old_task.id)
        if old.get('_conversation_id') != turn.conversation_id or old.get('_conversation_sequence') != turn.sequence:
            return None
        if not _versions(old.get('_record_dependencies', {})) or not _versions(old.get('_output_resources', {})):
            return None
        candidates = linked_versions(old)
        candidates.update(old.get('_output_resources', {}))
        for identifier, reference in old.get('_derived_task_sources', {}).items():
            if (not isinstance(reference, dict) or type(reference.get('version')) is not int
                    or reference['version'] < 1):
                return None
            candidates[identifier] = reference['version']
            derived.setdefault(identifier, []).append(reference)
        for identifier, version in candidates.items():
            versions.setdefault(identifier, set()).add(version)
    return versions, derived


def repair_one(app, db, actor, task):
    """只取消可证明多余且尚未出站的旧待办；不批准、不执行模型、不提交事务。"""
    instant = now()
    if (not task or task.owner_id != actor.user_id or task.household_id != actor.household_id
            or task.status != 'WAITING_CLIENT' or task.cancel_requested or task.deadline <= instant):
        return False
    scope(db, actor.user_id, actor.household_id)
    # 与客户端响应保持任务在前、待办在后的行锁顺序，避免并发确认形成死锁。
    task = db.scalar(select(Task).where(Task.id == task.id, Task.owner_id == actor.user_id,
        Task.household_id == actor.household_id).with_for_update().execution_options(populate_existing=True))
    if not task or task.status != 'WAITING_CLIENT' or task.cancel_requested or task.deadline <= now():
        return False
    body = app.vault.open(task.request, actor.user_id + ':task:' + task.id)
    # 已有审阅副本可能删掉原文或绑定一次性决定，不能通过重投影绕过该副本。
    if (not body.get('_agent') or not body.get('_conversation_id')
            or body.get('_history_scope_version') == 2 or body.get('_cloud_calls', 0) != 0
            or body.get('_privacy_review')
            or body.get('_cloud_consents') or body.get('record_ids') or body.get('_references')
            or body.get('_mentions') or body.get('_task_grants') or body.get('_reply_to_task_id')
            or body.get('steps') or body.get('_agent_batches') or body.get('_output_resources')
            or any(part.get('type') != 'text' for part in body.get('_parts', []))
            or not isinstance(body.get('message'), str) or private_text(body['message'])):
        return False
    context = body.get('_client_context', {})
    # 缺少精度和来源结构的旧位置不能推定为安全；保留其原待办交用户判断。
    if not isinstance(context, dict) or ('location' in context and context['location'] is not None):
        return False
    sequence = body.get('_conversation_sequence')
    if type(sequence) is not int or sequence < 1:
        return False
    conversation = db.get(Conversation, body['_conversation_id'])
    turn = db.scalar(select(ConversationTurn).where(ConversationTurn.task_id == task.id,
        ConversationTurn.owner_id == actor.user_id, ConversationTurn.household_id == actor.household_id,
        ConversationTurn.conversation_id == body['_conversation_id'], ConversationTurn.sequence == sequence))
    if (not conversation or conversation.owner_id != actor.user_id
            or conversation.household_id != actor.household_id or not turn):
        return False
    for model in (ModelUsage, Disclosure, Invocation):
        if db.scalar(select(model.id).where(model.task_id == task.id).limit(1)):
            return False
    wait = body.get('_client_wait')
    if not isinstance(wait, dict) or not isinstance(wait.get('action_id'), str):
        return False
    remaining = wait.get('remaining_seconds')
    if type(remaining) not in (int, float) or not math.isfinite(remaining) or remaining <= 0:
        return False
    action = db.scalar(select(ClientAction).where(ClientAction.id == wait['action_id']).with_for_update())
    if (not action or action.owner_id != actor.user_id or action.requester_id != actor.user_id
            or action.household_id != actor.household_id or action.task_id != task.id
            or action.kind != 'cloud.disclose' or action.status != 'PENDING'
            or action.expires_at <= instant or action.invocation_id or action.response):
        return False
    parameters = payload_of(app, action).get('parameters', {})
    old_versions = body.get('_record_dependencies', {})
    consent_versions = parameters.get('record_versions')
    if not _versions(old_versions) or not _versions(consent_versions):
        return False
    proof = _historical_proof(app, db, actor, task, body)
    if proof is None:
        return False
    proven, derived = proof
    if any(version not in proven.get(identifier, set())
           for identifier, version in {**old_versions, **consent_versions}.items()):
        return False
    if any(old_versions.get(identifier) != version for identifier, version in consent_versions.items()):
        return False
    if any(reference not in derived.get(identifier, [])
           for identifier, reference in body.get('_derived_task_sources', {}).items()):
        return False
    if any(identifier not in proven for identifier in body.get('_historical_record_ids', [])):
        return False

    candidate = copy.deepcopy(body)
    for key in list(candidate):
        if (key.startswith('_history_') and key != '_history_snapshot') or key in {
                '_record_dependencies', '_derived_task_sources', '_historical_record_ids'}:
            candidate.pop(key, None)
    try:
        history(app, db, actor, candidate)
        if candidate.get('_history_requires_disclosure'):
            return False
        from .client_actions import read_authorized_record
        for identifier in set(consent_versions) & set(candidate.get('_record_dependencies', {})):
            record = read_authorized_record(db, actor, identifier, candidate)
            if (record.owner_id != actor.user_id or record.sensitivity in {'SECRET', 'SENSITIVE'}
                    or record.cloud_policy == 'LOCAL_ONLY'):
                return False
    except HTTPException:
        return False
    removed = set(old_versions) - set(candidate.get('_record_dependencies', {}))
    omitted_turns = {entry['id'] for entry in body['_history_snapshot']} - {
        entry['id'] for entry in candidate.get('_history_context_snapshot', [])}
    if not removed and not omitted_turns:
        return False
    if task.cancel_requested or task.deadline <= now() or action.expires_at <= now():
        return False
    for key in ('_client_wait', '_privacy_review', '_privacy_failure'):
        candidate.pop(key, None)
    candidate['_task_id'] = task.id
    action.status = 'CANCELED'
    _response_notice(db, actor, action)
    task.request = app.vault.seal(candidate, actor.user_id + ':task:' + task.id)
    task.status, task.error = 'RECEIVED', None
    task.deadline = now() + max(1, min(3600, remaining))
    audit(db, actor, 'privacy.history_scope_repaired', task.id,
          {'canceled_action_id': action.id, 'removed_source_count': len(removed), 'scope_version': 2})
    emit(db, actor, 'task.updated', task.id)
    return True
