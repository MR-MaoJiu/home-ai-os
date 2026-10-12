"""旧失败健康请求的严格维护边界；不放宽正常重试与资料授权。"""
import json

import pytest
from sqlalchemy import select

from homeai.crypto import canonical, digest
from homeai.db import Device, Invocation, Task, scope, uid
from homeai.health_task_repair import requeue_invalid_health_window
from homeai.security import Actor
from test_health_plan_recovery import health_call, new_task


def seed_old(system, alice):
    task_id = new_task(alice)
    app = system[0].state
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, task_id)
        body = app.vault.open(task.request, alice.user_id + ':task:' + task.id)
        call = health_call(future=True)
        args = json.loads(call['function']['arguments'])
        body['steps'] = [{'capability': 'client.request@v1', 'arguments': args}]
        body['_model_rounds'], body['_cloud_calls'], body['_model_token_charge'] = 1, 1, 100
        body['_agent_batches'] = [{'steps': [0], 'message': {'role': 'assistant', 'content': '', 'tool_calls': [call]}}]
        row = Invocation(id=uid(), owner_id=alice.user_id, household_id='h1', task_id=task.id,
            step=0, capability='client.request@v1', status='FAILED', arguments_hash=digest(canonical(args)), arguments='')
        row.arguments = app.vault.seal(args, alice.user_id + ':invocation:' + row.id)
        db.add(row)
        task.status, task.error = 'FAILED', '健康数据时间范围无效或超过31天'
        task.request = app.vault.seal(body, alice.user_id + ':task:' + task.id)
        db.commit()
    return task_id


def actor(alice):
    return Actor(alice.user_id, 'h1', alice.device_id, 'infrastructure_owner')


def test_repair_retains_original_plan_budget_and_id_and_is_one_time(system, alice):
    task_id = seed_old(system, alice)
    app = system[0].state
    with system[2]() as db:
        task = db.get(Task, task_id)
        before = app.vault.open(task.request, alice.user_id + ':task:' + task_id)
        row = db.scalar(select(Invocation).where(Invocation.task_id == task_id))
        original = row.arguments
        assert requeue_invalid_health_window(app, db, actor(alice), task_id)
        db.commit()
        body = app.vault.open(task.request, alice.user_id + ':task:' + task_id)
        assert body['steps'] == before['steps']
        assert (body['_model_rounds'], body['_cloud_calls'], body['_model_token_charge']) == (1, 1, 100)
        assert task.status == 'RECEIVED' and row.status == 'SKIPPED' and row.arguments == original
        result = app.vault.open(row.result, alice.user_id + ':invocation-result:' + row.id)
        assert result['not_dispatched'] and result['status'] == 'not_executed'
        assert not requeue_invalid_health_window(app, db, actor(alice), task_id)


@pytest.mark.parametrize('condition', ['canceled', 'device_revoked', 'already_dispatched', 'result_exists',
    'changed_plan', 'review', 'other_member', 'extra_step', 'pending_action'])
def test_repair_cannot_reopen_unsafe_or_changed_state(system, alice, condition):
    task_id = seed_old(system, alice)
    app = system[0].state
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, task_id)
        row = db.scalar(select(Invocation).where(Invocation.task_id == task_id))
        body = app.vault.open(task.request, alice.user_id + ':task:' + task_id)
        if condition == 'canceled': task.cancel_requested = True
        if condition == 'device_revoked': db.get(Device, alice.device_id).revoked = True
        if condition == 'already_dispatched': row.status = 'NEEDS_RECONCILIATION'
        if condition == 'result_exists': row.result = app.vault.seal({}, alice.user_id + ':invocation-result:' + row.id)
        if condition == 'changed_plan': row.arguments_hash = 'changed'
        if condition == 'review': body['_privacy_review'] = {'status': 'pending'}
        if condition == 'other_member': row.owner_id = 'other'
        if condition == 'extra_step': body['steps'].append({'capability': 'reminder.create@v1', 'arguments': {'title': '不能重放'}})
        if condition == 'pending_action':
            from homeai.client_actions import create_request
            task.status = 'RECEIVED'
            create_request(app, db, actor(alice), task, 'provide_text', '尚未回答的问题', {'prompt': '请补充'}, request_key='existing')
            task.status, task.error = 'FAILED', '健康数据时间范围无效或超过31天'
        task.request = app.vault.seal(body, alice.user_id + ':task:' + task_id)
        db.commit()
        assert not requeue_invalid_health_window(app, db, actor(alice), task_id)
