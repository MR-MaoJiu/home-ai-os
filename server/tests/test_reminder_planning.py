"""提醒目标不能被无时间待办、一次提醒或第一个复合步骤冒充完成。"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from homeai.reminders import explicit_due_at, planned_reminder_requirement, validate_plan
from homeai.task_goals import reminder_satisfied, single_reminder
from homeai.planner import requires_live_tools


@pytest.mark.parametrize(('message', 'expected'), [
    ('帮我创建提醒买牛奶', 'untimed'),
    ('明天八点提醒我喝水', 'scheduled'),
    ('十分钟后叫我', 'scheduled'),
    ('每晚十一点提醒我睡觉', 'recurring'),
    ('以后叫我小王', None),
    ('你怎么不叫我名字', None),
    ('明天再跟你说', None),
    ('你怎么没有提醒我', None),
    ('取消明天提醒我喝水的任务', None),
    ('刚才让我每天提醒的事情是什么', None),
])
def test_only_explicit_time_and_action_require_a_scheduled_reminder(message, expected):
    assert planned_reminder_requirement(message) == expected


@pytest.mark.parametrize('arguments', [
    {'title': '喝水'},
    {'title': '喝水', 'due_at': '2026-11-01T08:00:00+08:00'},
    {'title': '喝水', 'due_at': '2026-11-01T08:00:00+08:00', 'notify_at_due': False},
])
def test_timed_request_cannot_be_saved_without_due_notification(arguments):
    with pytest.raises(HTTPException, match='schedule_reminder'):
        validate_plan('明天八点提醒我喝水', [('reminder.create@v1', arguments)])


def test_valid_scheduled_reminder_and_explicit_untimed_item_remain_supported():
    validate_plan('明天八点提醒我喝水', [('reminder.create@v1', {
        'title': '喝水', 'due_at': '2026-11-01T08:00:00+08:00', 'notify_at_due': True})])
    validate_plan('添加一条提醒买牛奶', [('reminder.create@v1', {'title': '买牛奶'})])
    validate_plan('明天八点提醒我喝水', [('client.request@v1', {'data_type': 'clarification'})])
    with pytest.raises(HTTPException, match='提醒事项'):
        validate_plan('明天上午10点提醒我吧', [('reminder.create@v1', {
            'title': '明天上午10点提醒', 'due_at': '2026-11-01T02:00:00Z', 'notify_at_due': True})])
    # 对话上下文明确的事项可以复用，无须因为当前句子是省略句重复询问。
    validate_plan('明天上午10点提醒我吧', [('reminder.create@v1', {
        'title': '带公开测试文档', 'due_at': '2026-11-01T02:00:00Z', 'notify_at_due': True})])


def test_periodic_reminder_requires_automation_but_mixed_requests_are_not_rewritten():
    with pytest.raises(HTTPException, match='create_automation'):
        validate_plan('每天八点提醒我喝水', [('reminder.create@v1', {
            'title': '喝水', 'due_at': '2026-11-01T08:00:00+08:00', 'notify_at_due': True})])
    validate_plan('每天提醒我喝水，另外明天提醒我出门', [('reminder.create@v1', {
        'title': '出门', 'due_at': '2026-11-01T08:00:00+08:00', 'notify_at_due': True})])


@pytest.mark.parametrize('message', [
    '明天提醒我喝水，顺便总结我的睡眠',
    '设置提醒并且分析今天的运动',
    '设置提醒，然后告诉我天气',
    '设置一条提醒，还要计算这周开销',
    '每天提醒我喝水',
    '设置两条提醒',
    '明天上午十点和下午三点提醒我喝水',
])
def test_first_reminder_does_not_finish_composite_or_recurring_request(message):
    assert not single_reminder(message)
    body = {'message': message, 'steps': [{'capability': 'reminder.create@v1'}]}
    assert not reminder_satisfied(body, {0: SimpleNamespace(status='SUCCEEDED')})


def test_one_successful_reminder_still_prevents_duplicate_effects():
    body = {'message': '明天八点提醒我喝水', 'steps': [{'capability': 'reminder.create@v1'}]}
    assert reminder_satisfied(body, {0: SimpleNamespace(status='SUCCEEDED')})
    assert not reminder_satisfied(body, {0: SimpleNamespace(status='FAILED')})
    assert not reminder_satisfied(body, {})
    assert single_reminder('十分钟后叫我起床')


def test_timed_wakeup_requires_tools_but_nickname_does_not():
    assert requires_live_tools('十分钟后叫我')
    assert requires_live_tools('明天八点喊我起床')
    assert not requires_live_tools('以后叫我小王')
    assert not requires_live_tools('你怎么没有提醒我')


@pytest.mark.parametrize(('message', 'zone', 'expected'), [
    ('明天上午10点提醒我喝水', 'Asia/Shanghai', '2026-10-13T02:00:00Z'),
    ('明天下午三点半提醒我喝水', 'Asia/Shanghai', '2026-10-13T07:30:00Z'),
    ('后天上午十点十五分提醒我出门', 'Asia/Shanghai', '2026-10-14T02:15:00Z'),
    ('十分钟后叫我起床', 'Asia/Shanghai', '2026-10-12T16:05:00Z'),
    ('半小时后提醒我喝水', 'Asia/Shanghai', '2026-10-12T16:25:00Z'),
    ('明天上午10点提醒我喝水', 'America/New_York', '2026-10-13T14:00:00Z'),
    ('明天八点提醒我喝水', 'Asia/Shanghai', None),
    ('有空时提醒我喝水', 'Asia/Shanghai', None),
    ('明天上午10点提醒我喝水，另外晚上8点提醒我运动', 'Asia/Shanghai', None),
    ('明天上午10点或者11点提醒我喝水', 'Asia/Shanghai', None),
    ('明天上午10点，或者下午3点提醒我喝水', 'Asia/Shanghai', None),
    ('明天上午十点和下午三点提醒我喝水', 'Asia/Shanghai', None),
    ('今天和明天上午10点提醒我喝水', 'Asia/Shanghai', None),
    ('1小时30分钟后提醒我喝水', 'Asia/Shanghai', None),
    ('一小时三十分钟后提醒我喝水', 'Asia/Shanghai', None),
    ('明天10分钟后提醒我喝水', 'Asia/Shanghai', None),
    ('每天上午10点提醒我喝水', 'Asia/Shanghai', None),
])
def test_explicit_time_uses_original_request_timezone_without_guessing(message, zone, expected):
    from datetime import datetime
    created = datetime.fromisoformat('2026-10-12T23:55:00+08:00').timestamp()
    assert explicit_due_at(message, created, zone) == expected


def test_relative_time_is_not_reset_when_missing_subject_is_answered_later():
    from datetime import datetime
    created = datetime.fromisoformat('2026-10-12T23:55:00+08:00').timestamp()
    # 补充多久后抵达不参与计算，调用方始终传 task.created_at。
    assert explicit_due_at('十分钟后提醒我吧', created, 'Asia/Shanghai') == '2026-10-12T16:05:00Z'


def submit_reminder(alice, message):
    import uuid
    response = alice.request('POST', '/api/v1/tasks', {'idempotency_key': str(uuid.uuid4()),
        'message': message, 'max_model_tokens': 262144})
    assert response.status_code == 202
    return response.json()['id']


@pytest.mark.asyncio
async def test_missing_subject_becomes_persistent_question_without_reasking_time(system, alice):
    from homeai.runtime import run_task
    from test_task_clarification import setup_planner, tool, snapshot
    calls, _ = setup_planner(system, [
        {'content': '要提醒你什么事，什么时间？'},
        {'content': None, 'tool_calls': [tool('ask_user', {'question': '需要提醒你做什么？'})]},
    ])
    identifier = submit_reminder(alice, '明天上午10点提醒我吧')
    await run_task(system[0].state, identifier, alice.user_id)
    status, body = snapshot(system, alice, identifier)
    assert status == 'RECEIVED' and body['_missing_reminder_repairs'] == 1
    assert not body['steps']
    for _ in range(3):
        await run_task(system[0].state, identifier, alice.user_id)
        status, body = snapshot(system, alice, identifier)
        if status == 'WAITING_CLIENT':
            break
    assert status == 'WAITING_CLIENT'
    request = alice.request('GET', '/api/v1/client-actions/' + body['_client_wait']['action_id']).json()
    assert request['purpose'] == '需要提醒你做什么？'
    assert '已有时间和时区就继续沿用' in calls[1]['messages'][0]['content']


@pytest.mark.asyncio
async def test_time_only_title_becomes_one_core_question_without_creating_empty_reminder(system, alice):
    from homeai.runtime import run_task
    from test_task_clarification import setup_planner, tool, snapshot
    calls, _ = setup_planner(system, [{'content': None, 'tool_calls': [tool('schedule_reminder', {
        'title': '明天上午10点提醒', 'due_at': '2026-11-01T02:00:00Z', 'notify_at_due': True})]}])
    identifier = submit_reminder(alice, '明天上午10点提醒我吧')
    for _ in range(3):
        await run_task(system[0].state, identifier, alice.user_id)
        status, body = snapshot(system, alice, identifier)
        if status == 'WAITING_CLIENT':
            break
    assert status == 'WAITING_CLIENT' and body['_reminder_subject_requested'] is True
    assert len(calls) == 1
    assert [step['capability'] for step in body['steps']] == ['client.request@v1']
    action = alice.request('GET', '/api/v1/client-actions/' + body['_client_wait']['action_id']).json()
    assert action['purpose'] == '需要提醒你做什么？'


@pytest.mark.asyncio
@pytest.mark.parametrize(('bad_tool', 'bad_arguments'), [
    ('create_reminder', {'title': '公开测试喝水'}),
    ('schedule_reminder', {'title': '公开测试喝水', 'due_at': '2026-11-01T08:00:00+08:00'}),
    ('schedule_reminder', {'title': '公开测试喝水', 'due_at': '2026-11-01T08:00:00', 'notify_at_due': True}),
])
async def test_bad_timed_plan_is_repaired_before_any_reminder_is_created(system, alice, bad_tool, bad_arguments):
    from sqlalchemy import select
    from homeai.db import Record, scope
    from homeai.runtime import run_task
    from test_task_clarification import setup_planner, tool, snapshot
    setup_planner(system, [
        {'content': None, 'tool_calls': [tool(bad_tool, bad_arguments)]},
        {'content': None, 'tool_calls': [tool('schedule_reminder', {
            'title': '公开测试喝水', 'due_at': '2026-11-01T08:00:00+08:00', 'notify_at_due': True})]},
        {'content': '提醒已设好。'},
    ])
    identifier = submit_reminder(alice, '明天八点提醒我喝水')
    await run_task(system[0].state, identifier, alice.user_id)
    status, body = snapshot(system, alice, identifier)
    assert status == 'RECEIVED' and not body['steps'] and body['_reminder_argument_repairs'] == 1
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        assert not list(db.scalars(select(Record).where(Record.kind == 'reminder.item')))
    for _ in range(5):
        await run_task(system[0].state, identifier, alice.user_id)
        status, body = snapshot(system, alice, identifier)
        if status == 'SUCCEEDED':
            break
    assert status == 'SUCCEEDED'
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        rows = list(db.scalars(select(Record).where(Record.kind == 'reminder.item')))
        assert len(rows) == 1
        payload = system[0].state.vault.open(rows[0].payload, alice.user_id + ':record:' + rows[0].id)
        assert payload['notify_at_due'] is True


@pytest.mark.asyncio
async def test_no_tool_promise_never_counts_as_successful_reminder(system, alice):
    from homeai.runtime import run_task
    from test_task_clarification import setup_planner, snapshot
    setup_planner(system, [{'content': '已经设置好了，到时叫你。'}])
    identifier = submit_reminder(alice, '十分钟后叫我')
    for _ in range(4):
        await run_task(system[0].state, identifier, alice.user_id)
        status, body = snapshot(system, alice, identifier)
        if status == 'FAILED':
            break
    assert status == 'FAILED' and body['_missing_reminder_repairs'] == 2
    assert not body['steps']
