"""模型生成错误时间时先修正计划；测试替身不读取真实健康样本。"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, func

from homeai.client_actions import ClientAction
from homeai.db import Invocation, Record, Task, scope
from homeai.runtime import run_task
from test_task_clarification import setup_planner, tool, snapshot


def health_call(*, future=False, target='self', extra=False):
    end = datetime.now(timezone.utc).replace(microsecond=0)
    if future:
        end += timedelta(hours=3)
    parameters = {'types': ['steps'], 'start_at': (end-timedelta(days=7)).isoformat(), 'end_at': end.isoformat()}
    if extra:
        parameters['grant_all'] = True
    return tool('request_client_data', {'target_member_id': target, 'kind': 'read_health',
        'purpose': '读取指定时间的步数', 'parameters': parameters})


def new_task(alice, message='请读取最近七天的步数。'):
    response = alice.request('POST', '/api/v1/tasks', {'message': message,
        'idempotency_key': str(uuid.uuid4()), 'max_model_tokens': 262144})
    assert response.status_code == 202
    return response.json()['id']


def counts(system, task_id):
    with system[2]() as db:
        return (db.scalar(select(func.count()).select_from(Invocation).where(Invocation.task_id == task_id)),
            db.scalar(select(func.count()).select_from(ClientAction).where(ClientAction.task_id == task_id)))


async def test_future_health_window_replans_whole_batch_before_any_effect(system, alice):
    valid = health_call()
    calls, _ = setup_planner(system, [
        {'tool_calls': [tool('create_reminder', {'title': '这一批还不能执行'}), health_call(future=True)]},
        {'tool_calls': [valid]},
    ])
    task_id = new_task(alice, '请添加一条稍后自行查看的待办，再读取最近七天的步数。')
    await run_task(system[0].state, task_id, alice.user_id)
    status, body = snapshot(system, alice, task_id)
    assert status == 'RECEIVED' and body['_health_plan_repairs'] == 1 and body['steps'] == []
    assert body['_model_rounds'] == 1 and body['_model_token_charge'] > 0
    assert counts(system, task_id) == (0, 0)
    with system[2]() as db:
        assert db.scalar(select(func.count()).select_from(Record)) == 0
    await run_task(system[0].state, task_id, alice.user_id)
    assert '健康查询结束时间' in calls[1]['messages'][0]['content']
    await run_task(system[0].state, task_id, alice.user_id)
    status, body = snapshot(system, alice, task_id)
    assert status == 'WAITING_CLIENT' and len(body['steps']) == 1
    assert counts(system, task_id) == (1, 1)
    action_id = body['_client_wait']['action_id']
    detail = alice.request('GET', '/api/v1/client-actions/' + action_id).json()
    assert detail['parameters'] == json.loads(valid['function']['arguments'])['parameters']
    assert detail['status'] == 'PENDING' and detail['can_respond']
    assert '_health_plan_feedback' not in body


async def test_completed_effect_survives_health_replanning(system, alice):
    calls, _ = setup_planner(system, [
        {'tool_calls': [tool('create_reminder', {'title': '已经执行的提醒'})]},
        {'tool_calls': [health_call(future=True)]},
        {'tool_calls': [health_call()]},
    ])
    task_id = new_task(alice, '请添加一条稍后自行查看的待办，再读取最近七天的步数。')
    for _ in range(7):
        await run_task(system[0].state, task_id, alice.user_id)
        if snapshot(system, alice, task_id)[0] == 'WAITING_CLIENT':
            break
    assert snapshot(system, alice, task_id)[0] == 'WAITING_CLIENT'
    with system[2]() as db:
        records = list(db.scalars(select(Record).where(Record.kind == 'reminder.item')))
        assert len(records) == 1
        rows = list(db.scalars(select(Invocation).where(Invocation.task_id == task_id).order_by(Invocation.step)))
        assert len(rows) == 2 and rows[0].status == 'SUCCEEDED'
    assert len(calls) == 3


async def test_repeated_invalid_window_is_bounded_and_consumes_model_budget(system, alice):
    calls, _ = setup_planner(system, [{'tool_calls': [health_call(future=True)]}])
    task_id = new_task(alice)
    for _ in range(6):
        await run_task(system[0].state, task_id, alice.user_id)
    status, body = snapshot(system, alice, task_id)
    assert status == 'FAILED' and body['_health_plan_repairs'] == 2
    assert len(calls) == 3 and body['_model_rounds'] == 3
    assert counts(system, task_id) == (0, 0)


@pytest.mark.parametrize('options', [{'target': 'unknown-member'}, {'target': 'unknown-member', 'future': True}, {'extra': True}])
async def test_replanning_does_not_swallow_authority_or_unknown_fields(system, alice, options):
    calls, _ = setup_planner(system, [{'tool_calls': [health_call(**options)]}])
    task_id = new_task(alice)
    for _ in range(3):
        await run_task(system[0].state, task_id, alice.user_id)
    status, body = snapshot(system, alice, task_id)
    assert status == 'FAILED' and not body.get('_health_plan_repairs')
    assert len(calls) == 1 and counts(system, task_id)[1] == 0


async def test_cancel_between_replans_stays_canceled(system, alice):
    calls, _ = setup_planner(system, [{'tool_calls': [health_call(future=True)]}, {'tool_calls': [health_call()]}])
    task_id = new_task(alice)
    await run_task(system[0].state, task_id, alice.user_id)
    assert alice.request('POST', '/api/v1/tasks/' + task_id + '/cancel').status_code == 200
    await run_task(system[0].state, task_id, alice.user_id)
    assert snapshot(system, alice, task_id)[0] == 'CANCELED'
    assert len(calls) == 1 and counts(system, task_id) == (0, 0)
