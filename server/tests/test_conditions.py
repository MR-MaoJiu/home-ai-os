"""用真实数据库、策略与内部能力验证声明式条件，不伪造工具结果。"""
import os
import uuid
import pytest
from sqlalchemy import select
from homeai.runtime import run_task
from homeai.db import Invocation, Task, Principal, scope
from test_workflows import workflow, create

pytestmark = pytest.mark.skipif(os.getenv('HOMEAI_INTEGRATION') != '1', reason='需要真实 PostgreSQL 和 OPA')


def condition(step, operator, path=None, value=None, mode='all'):
    return {'mode': mode, 'predicates': [{'step': step, 'path': path or [], 'operator': operator, 'value': value}]}


@pytest.mark.asyncio
async def test_missing_calendar_skips_send_and_continues_once(workflow):
    app, user = workflow
    tid = create(user, [
        {'capability': 'calendar.search@v1', 'arguments': {'query': str(uuid.uuid4())}},
        {'capability': 'mail.send@v1', 'arguments': {'to': 'test@example.com', 'subject': '审批验证'}, 'when': condition(0, 'not_empty')},
        {'capability': 'reminder.create@v1', 'arguments': {'title': '没有日程，整理计划'}},
    ])
    await run_task(app, tid, user.user_id)
    await run_task(app, tid, user.user_id)
    steps = user.request('GET', '/api/v1/tasks/' + tid + '/steps').json()
    assert steps[1]['status'] == 'SKIPPED' and steps[1]['result'] is None
    assert user.request('GET', '/api/v1/approvals').json() == []
    # 从另一个应用实例恢复，跳过状态来自数据库，不依赖进程内变量。
    from homeai.api import create_app
    restarted = create_app(app.settings).state
    try:
        await run_task(restarted, tid, user.user_id)
        await run_task(restarted, tid, user.user_id)
    finally:
        restarted.db.kw['bind'].dispose()
    steps = user.request('GET', '/api/v1/tasks/' + tid + '/steps').json()
    assert [row['status'] for row in steps] == ['SUCCEEDED', 'SKIPPED', 'SUCCEEDED']
    assert user.request('GET', '/api/v1/data/' + steps[2]['result']['record_id']).json()['payload']['title'] == '没有日程，整理计划'


@pytest.mark.asyncio
async def test_true_condition_still_requires_real_approval(workflow):
    app, user = workflow
    tid = create(user, [
        {'capability': 'reminder.create@v1', 'arguments': {'title': '准备报告'}},
        {'capability': 'mail.send@v1', 'arguments': {'to': 'test@example.com', 'subject': '审批验证'}, 'when': condition(0, 'equals', ['status'], 'stored')},
    ])
    await run_task(app, tid, user.user_id)
    await run_task(app, tid, user.user_id)
    assert user.request('GET', '/api/v1/tasks/' + tid).json()['status'] == 'AWAITING_APPROVAL'
    assert len(user.request('GET', '/api/v1/approvals').json()) == 1
    assert user.request('POST', '/api/v1/tasks/' + tid + '/cancel').status_code == 200


@pytest.mark.asyncio
async def test_skipped_result_reference_fails_without_second_effect(workflow):
    app, user = workflow
    tid = create(user, [
        {'capability': 'calendar.search@v1'},
        {'capability': 'reminder.create@v1', 'when': condition(0, 'not_empty')},
        {'capability': 'reminder.create@v1', 'arguments': {'title': {'$step': 1, 'path': ['record_id']}}},
    ])
    for _ in range(3):
        await run_task(app, tid, user.user_id)
    assert user.request('GET', '/api/v1/tasks/' + tid).json()['status'] == 'FAILED'
    assert len(user.request('GET', '/api/v1/tasks/' + tid + '/steps').json()) == 2


def test_contract_rejects_future_reference_code_and_boolean_numeric(workflow):
    _, user = workflow
    for gate in [condition(1, 'exists'), condition(0, 'gt', value=True),
                 {'mode': 'eval', 'predicates': [{'step': 0, 'operator': 'exists'}]}]:
        steps = [{'capability': 'calendar.search@v1'}, {'capability': 'reminder.create@v1', 'when': gate}]
        assert user.request('POST', '/api/v1/tasks', {'idempotency_key': str(uuid.uuid4()), 'steps': steps}).status_code == 422
        assert user.request('POST', '/api/v1/automations', {'name': '无效条件', 'cron': '* * * * *', 'skill': {'name': '验证', 'steps': steps}}).status_code == 422


@pytest.mark.asyncio
async def test_comparisons_missing_path_and_all_any(workflow):
    app, user = workflow
    tid = create(user, [
        {'capability': 'calendar.create@v1', 'arguments': {'title': '条件检验', 'count': 3, 'confirmed': True}},
        {'capability': 'calendar.search@v1', 'arguments': {'query': '条件检验'}},
        {'capability': 'reminder.create@v1', 'arguments': {'title': '数值匹配'}, 'when': {'mode': 'all', 'predicates': [
            {'step': 1, 'path': [0, 'payload', 'count'], 'operator': 'gte', 'value': 3},
            {'step': 1, 'path': [0, 'payload', 'missing'], 'operator': 'not_exists'}]}},
        {'capability': 'reminder.create@v1', 'arguments': {'title': '布尔不能匹配数字'}, 'when': condition(1, 'equals', [0, 'payload', 'confirmed'], 1)},
        {'capability': 'reminder.create@v1', 'arguments': {'title': '任一匹配'}, 'when': {'mode': 'any', 'predicates': [
            {'step': 3, 'operator': 'exists'}, {'step': 2, 'path': ['status'], 'operator': 'equals', 'value': 'stored'}]}},
    ])
    for _ in range(5):
        await run_task(app, tid, user.user_id)
    steps = user.request('GET', '/api/v1/tasks/' + tid + '/steps').json()
    assert [row['status'] for row in steps] == ['SUCCEEDED', 'SUCCEEDED', 'SUCCEEDED', 'SKIPPED', 'SUCCEEDED']
