"""旧历史污染维护入口的隔离测试；不调用真实模型或维护运行库。"""
import pytest
from sqlalchemy import func, select

from conftest import SignedClient
from homeai.client_actions import ClientAction, create_request
from homeai.db import Audit, Disclosure, Invocation, Notification, Outbox, Task, now, scope
from homeai.history_scope_repair import repair_one
from homeai.model_routing import ModelUsage
from homeai.security import Actor
from test_conversations import finish, new_chat, send
from test_security_data import put, record


def actor(client):
    return Actor(client.user_id, 'h1', client.device_id, 'infrastructure_owner')


def legacy_wait(system, alice, *, provenance='dependencies', bare=False):
    app = system[0].state
    identifier = new_chat(alice)
    source = put(alice, record(kind='health.snapshot', payload={'content': '人工构造的健康测试样本'}))
    first = send(alice, identifier, '分析这份健康记录。').json()
    finish(system, alice, first['task_id'], '仅用于隔离回归的旧敏感分析结果。')
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        previous = db.get(Task, first['task_id'])
        value = app.vault.open(previous.request, alice.user_id + ':task:' + previous.id)
        if not bare:
            if provenance == 'dependencies':value['_record_dependencies'] = {source: 1}
            elif provenance == 'outputs':value['_output_resources'] = {source: 1}
            else:value['_parts'] = [{'type': 'file', 'record_id': source, 'version': 1}]
        previous.request = app.vault.seal(value, alice.user_id + ':task:' + previous.id)
        db.commit()
    current = send(alice, identifier, '解释二叉树的结构。').json()['task_id']
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, current)
        value = app.vault.open(task.request, alice.user_id + ':task:' + task.id)
        value['_record_dependencies'] = {} if bare else {source: 1}
        value['_historical_record_ids'] = [] if bare else [source]
        value['_history_summary_sources'] = ['obsolete-excerpt']
        value['_history_followup_turn'] = 'obsolete-hint'
        value['_privacy_failure'] = {'code': 'old-context'}
        value['_client_context'] = {'battery_level': 0.5, 'network_type': 'wifi'}
        value['_model_token_charge'], value['_model_rounds'], value['_cloud_token_charge'] = 17, 0, 0
        task.request = app.vault.seal(value, alice.user_id + ':task:' + task.id)
        created = create_request(app, db, actor(alice), task, 'cloud.disclose', '历史污染授权',
            {'scope_hash': 'a' * 64, 'record_versions': value['_record_dependencies'], 'provider_id': 'cloud.test'})
        db.commit()
        return current, created['client_action_id'], source, first['task_id']


@pytest.mark.parametrize('provenance', ['dependencies', 'parts', 'outputs'])
def test_repair_cancels_only_unneeded_pending_card_and_preserves_budget(system, alice, provenance):
    task_id, action_id, source, _ = legacy_wait(system, alice, provenance=provenance)
    app = system[0].state
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, task_id)
        before = app.vault.open(task.request, alice.user_id + ':task:' + task.id)
        initial_event_count = db.scalar(select(func.count()).select_from(Outbox))
        assert repair_one(app, db, actor(alice), task)
        db.flush()
        after = app.vault.open(task.request, alice.user_id + ':task:' + task.id)
        action = db.get(ClientAction, action_id)
        assert task.status == 'RECEIVED' and task.error is None
        assert now() < task.deadline <= now() + before['_client_wait']['remaining_seconds']
        assert action.status == 'CANCELED' and action.response is None and action.responded_at is None
        assert db.get(Notification, action.notification_id).status == 'CANCELED'
        assert after['_history_scope_version'] == 2 and not after['_history_requires_disclosure']
        assert source not in after.get('_record_dependencies', {})
        assert not any(key in after for key in ('_client_wait', '_privacy_review', '_privacy_failure', '_cloud_consents'))
        assert after['message'] == before['message'] and after['_client_context'] == before['_client_context']
        for key in ('_model_token_charge', '_model_rounds', '_cloud_token_charge', 'max_model_tokens'):
            assert after[key] == before[key]
        assert db.scalar(select(func.count()).select_from(Outbox)) == initial_event_count + 2
        assert db.scalar(select(Audit).where(Audit.resource_id == task.id, Audit.action == 'privacy.history_scope_repaired'))
        assert not repair_one(app, db, actor(alice), task)
        # 此函数不自行提交；维护者仍可回滚全部状态。
        db.rollback()
        assert db.get(Task, task_id).status == 'WAITING_CLIENT'
        assert db.get(ClientAction, action_id).status == 'PENDING'


def test_repair_bare_sensitive_history_without_record_does_not_approve_cloud(system, alice):
    task_id, action_id, _, _ = legacy_wait(system, alice, bare=True)
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, task_id)
        assert repair_one(system[0].state, db, actor(alice), task)
        value = system[0].state.vault.open(task.request, alice.user_id + ':task:' + task.id)
        assert value['_history_context_snapshot'] == []
        assert not value.get('_cloud_consents')
        assert db.get(ClientAction, action_id).status == 'CANCELED'


@pytest.mark.parametrize('case', [
    'current_record', 'current_attachment', 'current_reference', 'mention', 'grant', 'reply',
    'private_message', 'precise_location', 'unknown_location', 'empty_location', 'canceled_task', 'expired_task',
    'expired_action', 'processed_action', 'foreign_action', 'old_consent', 'already_scoped',
    'cloud_calls', 'model_usage', 'disclosure', 'invocation', 'unproven_source',
    'unproven_derived', 'unproven_historical_reference', 'wrong_snapshot', 'changed_snapshot',
    'real_followup',
])
def test_repair_refuses_active_scope_or_uncertain_provenance_without_mutation(system, alice, case):
    task_id, action_id, source, previous_id = legacy_wait(system, alice)
    app = system[0].state
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, task_id)
        value = app.vault.open(task.request, alice.user_id + ':task:' + task.id)
        action = db.get(ClientAction, action_id)
        if case == 'current_record':value['record_ids'] = [source]
        elif case == 'current_attachment':value['_parts'] = [{'type': 'image', 'record_id': source, 'version': 1}]
        elif case == 'current_reference':value['_references'] = [{'record_id': source, 'version': 1}]
        elif case == 'mention':value['_mentions'] = [{'member_id': 'other'}]
        elif case == 'grant':value['_task_grants'] = {source: 'grant'}
        elif case == 'reply':value['_reply_to_task_id'] = previous_id
        elif case == 'private_message':value['message'] = '分析我本次血压记录。'
        elif case == 'precise_location':value['_client_context']['location'] = {'precision': 'precise', 'latitude': 12.3}
        elif case == 'unknown_location':value['_client_context']['location'] = {'value': 'unknown'}
        elif case == 'empty_location':value['_client_context']['location'] = {}
        elif case == 'canceled_task':task.cancel_requested = True
        elif case == 'expired_task':task.deadline = now() - 1
        elif case == 'expired_action':action.expires_at = now() - 1
        elif case == 'processed_action':action.status = 'RESPONDED'
        elif case == 'foreign_action':action.requester_id = 'another-member'
        elif case == 'old_consent':value['_cloud_consents'] = ['past-consent']
        elif case == 'already_scoped':value['_history_scope_version'] = 2
        elif case == 'cloud_calls':value['_cloud_calls'] = 1
        elif case == 'model_usage':db.add(ModelUsage(owner_id=alice.user_id, household_id='h1', task_id=task_id,
            call_key='prior-dispatch', day='2026-10-02', reserved=5, charged=5, status='RESERVED'))
        elif case == 'disclosure':db.add(Disclosure(owner_id=alice.user_id, household_id='h1', task_id=task_id,
            provider_id='cloud.test', categories='[]', bytes_sent=5, status='FAILED'))
        elif case == 'invocation':db.add(Invocation(owner_id=alice.user_id, household_id='h1', task_id=task_id,
            step=0, capability='memory.search@v1', arguments='encrypted', arguments_hash='hash', status='PENDING'))
        elif case == 'unproven_source':value['_record_dependencies']['not-in-history'] = 1
        elif case == 'unproven_derived':value['_derived_task_sources'] = {'unproven': {'source_id': source, 'version': 1, 'source_version': 1}}
        elif case == 'unproven_historical_reference':value['_historical_record_ids'] = ['not-in-history']
        elif case == 'wrong_snapshot':value['_history_snapshot'][0]['id'] = 'another-turn'
        elif case == 'changed_snapshot':value['_history_snapshot'][0]['result_hash'] = 'changed'
        elif case == 'real_followup':value['message'] = '刚才那份报告继续分析。'
        task.request = app.vault.seal(value, alice.user_id + ':task:' + task.id)
        db.flush()
        before = (task.request, task.status, task.deadline, action.status, action.response,
                  db.scalar(select(func.count()).select_from(Outbox)), db.scalar(select(func.count()).select_from(Audit)))
        assert not repair_one(app, db, actor(alice), task), case
        db.flush()
        after = (task.request, task.status, task.deadline, action.status, action.response,
                 db.scalar(select(func.count()).select_from(Outbox)), db.scalar(select(func.count()).select_from(Audit)))
        assert after == before


def test_repair_refuses_another_member_or_household_task(system, alice):
    task_id, _, _, _ = legacy_wait(system, alice)
    bob = SignedClient(system[1], system[2])
    with system[2]() as db:
        task = db.get(Task, task_id)
        assert not repair_one(system[0].state, db, actor(bob), task)
        assert not repair_one(system[0].state, db,
            Actor(alice.user_id, 'other-household', alice.device_id, 'infrastructure_owner'), task)


@pytest.mark.parametrize('review', [
    {'id': 'pending-review', 'status': 'pending', 'candidate': {'text': '尚待核对的副本'}},
    {'id': 'approved-review', 'status': 'approved', 'candidate': {'text': '已确认的副本'}},
    {'id': 'edited-review', 'decision': 'edit', 'candidate': {'text': '用户已删除部分原文'}},
    {'id': 'forced-review', 'decision': 'force', 'candidate': {'text': '用户仅批准此副本'}},
])
def test_repair_preserves_any_existing_privacy_review_without_changes(system, alice, review):
    task_id, action_id, _, _ = legacy_wait(system, alice)
    app = system[0].state
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, task_id)
        body = app.vault.open(task.request, alice.user_id + ':task:' + task.id)
        body['_privacy_review'] = review
        task.request = app.vault.seal(body, alice.user_id + ':task:' + task.id)
        db.flush()
        action = db.get(ClientAction, action_id)
        before = (task.request, task.status, task.deadline, action.status, action.response,
                  db.scalar(select(func.count()).select_from(Outbox)), db.scalar(select(func.count()).select_from(Audit)))
        assert not repair_one(app, db, actor(alice), task)
        db.flush()
        after = (task.request, task.status, task.deadline, action.status, action.response,
                 db.scalar(select(func.count()).select_from(Outbox)), db.scalar(select(func.count()).select_from(Audit)))
        assert after == before
        assert app.vault.open(task.request, alice.user_id + ':task:' + task.id)['_privacy_review'] == review
