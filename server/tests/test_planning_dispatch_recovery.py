"""隔离故障注入验证未决规划恢复；不访问真实模型，也不修改运行中的任务。"""
import asyncio
import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from homeai.agent import advance
from homeai.db import Task, scope
from homeai.model_routing import ModelUsage
from homeai.providers import Registry
from homeai.security import Actor
from homeai.token_accounting import repair_unsettled_budget_task
from test_budget_settlement import configured, create, snapshot


def body_update(system, alice, task_id, **changes):
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, task_id)
        body = system[0].state.vault.open(task.request, alice.user_id + ':task:' + task_id)
        body.update(changes)
        task.request = system[0].state.vault.seal(body, alice.user_id + ':task:' + task_id)
        db.commit()


def assert_unknown_without_retry(system, alice, task_id, expected_error):
    status, body, usage = snapshot(system, alice, task_id)
    assert status == 'FAILED'
    with system[2]() as db:
        assert expected_error in db.get(Task, task_id).error
    assert len(usage) == 1 and usage[0]['status'] == 'UNKNOWN'
    assert usage[0]['charged'] == usage[0]['reserved'] == body['_cloud_token_charge']
    entry = body['_planning_usage'][task_id + ':plan:0']
    assert entry['status'] == 'UNKNOWN'
    assert body['_model_token_charge'] == entry['charged'] == entry['reserved']
    preview = alice.request('GET', f'/api/v1/tasks/{task_id}/retry').json()
    assert preview['can_retry'] is False and preview['code'] == 'planning_result_unknown'
    response = alice.request('POST', f'/api/v1/tasks/{task_id}/retry', {'idempotency_key': str(uuid.uuid4())})
    assert response.status_code == 409
    assert snapshot(system, alice, task_id) == (status, body, usage)


@pytest.mark.asyncio
async def test_http_timeout_preserves_cause_and_unknown_charge_without_second_plan(system, alice):
    _, calls = configured(system, alice, 'cloud', None, invalid_tools=False, unknown=True)
    task_id = create(alice, 'cloud')
    await advance(system[0].state, task_id, alice.user_id)
    assert_unknown_without_retry(system, alice, task_id, '云模型请求超时')
    await advance(system[0].state, task_id, alice.user_id)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('interruption', ['step_timeout', 'cancel'])
async def test_outer_timeout_or_cancellation_marks_dispatched_call_unknown(system, alice, interruption):
    configured(system, alice, 'cloud', None, invalid_tools=False)
    entered = asyncio.Event()
    calls = []

    async def transport(request):
        payload = json.loads(request.content)
        if request.url.host == 'privacy.test':
            review = '"safe"' in payload['messages'][0]['content']
            value = {'safe': True, 'uncertain': False} if review else {'entities': [], 'sensitive': False, 'uncertain': False}
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(value)}}]})
        assert request.url.host == 'model.example'
        calls.append(request.url.host)
        entered.set()
        await asyncio.Future()

    system[0].state.registry = Registry(system[0].state.vault, httpx.MockTransport(transport))
    task_id = create(alice, 'cloud')
    if interruption == 'step_timeout':
        body_update(system, alice, task_id, step_timeout_seconds=0.3)
        await asyncio.wait_for(advance(system[0].state, task_id, alice.user_id), 5)
        expected = '模型规划等待超时'
    else:
        running = asyncio.create_task(advance(system[0].state, task_id, alice.user_id))
        await asyncio.wait_for(entered.wait(), 5)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        expected = '模型规划执行中断'
    assert entered.is_set() and len(calls) == 1
    assert_unknown_without_retry(system, alice, task_id, expected)


def interrupted_task(system, alice, usage_status='RESERVED', task_status='EXECUTING', failure='task_tokens_exceeded'):
    _, calls = configured(system, alice, 'cloud', 8, invalid_tools=False)
    task_id = create(alice, 'cloud')
    key = task_id + ':plan:0'
    body_update(system, alice, task_id, max_model_tokens=65536, _model_rounds=1,
                _model_token_charge=37528, _cloud_calls=1, _cloud_token_charge=36308,
                _planning_usage={key: {'reserved': 37528, 'charged': 37528, 'status': 'RESERVED'}},
                _budget_failure={'code': failure, 'recoverable': False},
                _manual_retries=[{'key': 'legacy-retry', 'previous_error': '云模型请求超时'}])
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, task_id)
        task.status, task.error = task_status, '旧版覆盖后的预算提示'
        db.add(ModelUsage(owner_id=alice.user_id, household_id='h1', task_id=task_id,
            call_key=key, day='2026-10-02', reserved=36308, charged=36308, status=usage_status))
        db.commit()
    return task_id, calls


@pytest.mark.asyncio
@pytest.mark.parametrize('usage_status', ['RESERVED', 'UNKNOWN'])
async def test_process_recovery_checks_previous_dispatch_before_budget_or_new_request(system, alice, usage_status):
    task_id, calls = interrupted_task(system, alice, usage_status)
    before = snapshot(system, alice, task_id)
    await advance(system[0].state, task_id, alice.user_id)
    after = snapshot(system, alice, task_id)
    assert after[0] == 'FAILED' and not calls
    assert after[1:] == before[1:]
    with system[2]() as db:
        assert '上一次云模型规划未收到可确认的结果' in db.get(Task, task_id).error


@pytest.mark.parametrize('usage_status', ['RESERVED', 'UNKNOWN', 'SUCCEEDED'])
@pytest.mark.parametrize('failure', ['task_tokens_exceeded', 'member_daily_budget_exceeded'])
def test_legacy_budget_correction_is_narrow_idempotent_and_keeps_usage(system, alice, usage_status, failure):
    task_id, calls = interrupted_task(system, alice, usage_status, 'WAITING_BUDGET', failure)
    before = snapshot(system, alice, task_id)
    actor = Actor(alice.user_id, 'h1', alice.device_id, 'infrastructure_owner')
    expected = usage_status != 'SUCCEEDED' and failure == 'task_tokens_exceeded'
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, task_id)
        assert repair_unsettled_budget_task(system[0].state, db, actor, task) is expected
        assert repair_unsettled_budget_task(system[0].state, db, actor, task) is False
        db.commit()
    after = snapshot(system, alice, task_id)
    assert not calls and after[2] == before[2]
    assert after[1]['_planning_usage'] == before[1]['_planning_usage']
    assert after[1]['_model_token_charge'] == 37528
    assert after[1]['_cloud_token_charge'] == 36308
    if expected:
        assert after[0] == 'FAILED' and '_budget_failure' not in after[1]
        assert after[1]['_superseded_budget_failure'] == before[1]['_budget_failure']
    else:
        assert after == before
