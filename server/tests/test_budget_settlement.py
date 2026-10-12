"""只替换模型HTTP响应，在临时数据库验证实际用量结算；不调用付费服务。"""
import json
import uuid

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import event, select

from homeai.agent import advance
from homeai.cloud_gateway import invoke
from homeai.contracts import TaskRequest
from homeai.db import Device, Record, Task, scope
from homeai.model_routing import BudgetExceeded, ModelUsage
from homeai.providers import Registry
from homeai.runtime import run_task, submit
from homeai.security import Actor
from test_planning_resumption import setup_models
from test_security_data import put, record


def snapshot(system, user, identifier):
    with system[2]() as db:
        scope(db, user.user_id, 'h1')
        task = db.get(Task, identifier)
        body = system[0].state.vault.open(task.request, user.user_id + ':task:' + identifier)
        rows = list(db.scalars(select(ModelUsage).where(ModelUsage.task_id == identifier)))
        usage = [{'status': row.status, 'charged': row.charged, 'reserved': row.reserved, 'call_key': row.call_key} for row in rows]
        return task.status, body, usage


def configured(system, user, mode, actual, *, invalid_tools=True, before_response=None, unknown=False, tool_name=None):
    cloud, _, _ = setup_models(system, user)
    if mode == 'local':
        config = user.request('GET', '/api/v1/manage/models').json()['configuration']
        changed = user.request('PUT', '/api/v1/manage/models', {**config, 'cloud_planner': None, 'vision': None, 'shared_cloud': False})
        assert changed.status_code == 200, changed.text
    calls = []
    def transport(request):
        payload = json.loads(request.content)
        if request.url.host == 'privacy.test':
            review = '"safe"' in payload['messages'][0]['content']
            value = {'safe': True, 'uncertain': False} if review else {'entities': [], 'sensitive': False, 'uncertain': False}
            return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': json.dumps(value)}}]})
        assert request.url.host == ('model.example' if mode == 'cloud' else 'fast.test')
        calls.append(payload)
        if before_response:
            before_response()
        if unknown:
            raise httpx.ReadTimeout('隔离测试：模型响应结果未知', request=request)
        message = {'role': 'assistant', 'content': '公开测试答复'}
        if invalid_tools or tool_name:
            message['tool_calls'] = [{'id': 'invalid-test-tool', 'type': 'function',
                                      'function': {'name': 'nonexistent_operation' if invalid_tools else tool_name, 'arguments': '{}'}}]
        response = {'choices': [{'message': message}]}
        if actual is not None:
            response['usage'] = {'total_tokens': actual}
        return httpx.Response(200, json=response)
    app = system[0].state
    app.registry = Registry(app.vault, httpx.MockTransport(transport))
    return cloud, calls


def create(user, mode, record_ids=None):
    response = user.request('POST', '/api/v1/tasks', {'message': '请说明公开的先进先出队列。', 'mode': mode,
        'record_ids': record_ids or [], 'idempotency_key': str(uuid.uuid4()), 'max_output_tokens': 128, 'max_model_tokens': 262144})
    assert response.status_code == 202, response.text
    return response.json()['id']


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['local', 'cloud'])
@pytest.mark.parametrize('actual', [19, 90000])
async def test_known_actual_survives_tool_decoder_failure_and_can_exceed_reservation(system, alice, mode, actual):
    _, calls = configured(system, alice, mode, actual)
    task_id = create(alice, mode)
    await advance(system[0].state, task_id, alice.user_id)
    status, body, usage = snapshot(system, alice, task_id)
    assert status == 'FAILED' and len(calls) == 1
    assert body['_model_rounds'] == 1
    assert body['_model_token_charge'] == actual
    if mode == 'cloud':
        assert usage[0]['status'] == 'SUCCEEDED' and usage[0]['charged'] == actual
        assert body['_cloud_token_charge'] == actual
        if actual == 90000:
            assert actual > usage[0]['reserved']
    else:
        assert usage == []


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['local', 'cloud'])
@pytest.mark.parametrize('mutation', ['cancel', 'device_revoke', 'source_secret'])
async def test_known_usage_survives_response_time_cancellation_or_authorization_change(system, alice, mode, mutation):
    active = {}
    def change_authority():
        with system[2]() as db:
            scope(db, alice.user_id, 'h1')
            if mutation == 'cancel':
                task = db.get(Task, active['task_id'])
                task.cancel_requested, task.status = True, 'CANCELED'
            elif mutation == 'device_revoke':
                db.get(Device, alice.device_id).revoked = True
            else:
                db.get(Record, active['record_id']).sensitivity = 'SECRET'
            db.commit()
    _, calls = configured(system, alice, mode, 37, invalid_tools=False, before_response=change_authority)
    record_ids = []
    if mutation == 'source_secret':
        value = record(source_id='budget-public-source', kind='note', payload={'text': '公开测试来源'})
        value.update(sensitivity='PUBLIC', cloud_policy='REDACT_AND_ALLOW')
        active['record_id'] = put(alice, value)
        record_ids.append(active['record_id'])
    active['task_id'] = create(alice, mode, record_ids)
    await advance(system[0].state, active['task_id'], alice.user_id)
    status, body, usage = snapshot(system, alice, active['task_id'])
    assert status in {'CANCELED', 'FAILED'} and len(calls) == 1
    assert body['_model_token_charge'] == 37 and body['_model_rounds'] == 1
    if mode == 'cloud':
        assert usage[0]['status'] == 'SUCCEEDED' and usage[0]['charged'] == 37
        assert body['_cloud_token_charge'] == 37


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['local', 'cloud'])
async def test_later_budget_precheck_keeps_prior_actual_and_does_not_call_model(system, alice, mode):
    _, calls = configured(system, alice, mode, 29, invalid_tools=False, tool_name='list_automations')
    task_id = create(alice, mode)
    await advance(system[0].state, task_id, alice.user_id)
    assert snapshot(system, alice, task_id)[0] == 'RECEIVED'
    # 真实Core执行只读自动化列表，然后模拟下一轮任务预算已经收紧的状态。
    await run_task(system[0].state, task_id, alice.user_id)
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, task_id)
        body = system[0].state.vault.open(task.request, alice.user_id + ':task:' + task_id)
        assert body['_model_token_charge'] == 29
        body['max_model_tokens'] = 256
        task.request = system[0].state.vault.seal(body, alice.user_id + ':task:' + task_id)
        db.commit()
    await advance(system[0].state, task_id, alice.user_id)
    status, body, usage = snapshot(system, alice, task_id)
    assert status == 'WAITING_BUDGET'
    assert body['_model_token_charge'] == 29 and body['_model_rounds'] == 1
    assert len(calls) == 1
    if mode == 'cloud':
        assert body['_cloud_token_charge'] == 29 and usage[0]['charged'] == 29


@pytest.mark.asyncio
@pytest.mark.parametrize('actual', [23, 5000])
async def test_gateway_commits_usage_and_task_charge_in_same_transaction(system, alice, actual):
    manifest, calls = configured(system, alice, 'cloud', actual, invalid_tools=False)
    app = system[0].state
    actor = Actor(alice.user_id, 'h1', alice.device_id, 'infrastructure_owner')
    call_key = 'gateway-settlement-' + str(uuid.uuid4())
    with system[2]() as db:
        task = submit(db, actor, TaskRequest(message='公开网关结算测试', idempotency_key=str(uuid.uuid4())), app.vault)
        task_id = task.id
        db.commit()
    observed = []
    def after_commit(_session):
        # 用独立会话观察每次已提交状态，不能靠调用方最后补写掩盖事务间不一致。
        with system[2]() as check:
            scope(check, alice.user_id, 'h1')
            usage = check.scalar(select(ModelUsage).where(ModelUsage.call_key == call_key))
            if usage is not None and usage.status == 'SUCCEEDED':
                stored = check.get(Task, task_id)
                payload = app.vault.open(stored.request, actor.user_id + ':task:' + task_id)
                observed.append((usage.charged, payload['_cloud_token_charge']))
    event.listen(system[2].class_, 'after_commit', after_commit)
    try:
        with system[2]() as db:
            scope(db, actor.user_id, 'h1')
            task = db.get(Task, task_id)
            body = app.vault.open(task.request, actor.user_id + ':task:' + task_id)
            await invoke(app, db, actor, manifest, task, body,
                         {'messages': [{'role': 'user', 'content': '公开文字'}], 'max_tokens': 64}, call_key)
            db.rollback()
    finally:
        event.remove(system[2].class_, 'after_commit', after_commit)
    _, body, usage = snapshot(system, alice, task_id)
    assert len(calls) == 1 and observed
    assert all(pair == (actual, actual) for pair in observed)
    assert usage[0]['charged'] == body['_cloud_token_charge'] == actual
    if actual == 5000:
        assert usage[0]['charged'] > usage[0]['reserved']


@pytest.mark.asyncio
async def test_unknown_cloud_response_keeps_reservation_and_same_call_cannot_replay(system, alice):
    manifest, calls = configured(system, alice, 'cloud', 0, invalid_tools=False, unknown=True)
    app = system[0].state
    actor = Actor(alice.user_id, 'h1', alice.device_id, 'infrastructure_owner')
    call_key = 'unknown-call-' + str(uuid.uuid4())
    arguments = {'messages': [{'role': 'user', 'content': '公开测试'}], 'max_tokens': 64}
    with system[2]() as db:
        task = submit(db, actor, TaskRequest(message='结果未知测试', idempotency_key=str(uuid.uuid4())), app.vault)
        task_id = task.id
        db.commit()
        body = app.vault.open(task.request, actor.user_id + ':task:' + task_id)
        with pytest.raises(HTTPException) as failure:
            await invoke(app, db, actor, manifest, task, body, arguments, call_key)
        assert failure.value.status_code == 504
    _, first, usage = snapshot(system, alice, task_id)
    assert len(calls) == 1 and usage[0]['status'] == 'UNKNOWN'
    assert usage[0]['charged'] == usage[0]['reserved'] == first['_cloud_token_charge']
    with system[2]() as db:
        scope(db, actor.user_id, 'h1')
        task = db.get(Task, task_id)
        body = app.vault.open(task.request, actor.user_id + ':task:' + task_id)
        with pytest.raises(HTTPException) as duplicate:
            await invoke(app, db, actor, manifest, task, body, arguments, call_key)
        assert duplicate.value.status_code == 409
        assert not isinstance(duplicate.value, BudgetExceeded)
    _, later, usage = snapshot(system, alice, task_id)
    assert len(calls) == 1 and usage[0]['status'] == 'UNKNOWN'
    assert later['_cloud_token_charge'] == first['_cloud_token_charge']


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['local', 'cloud'])
@pytest.mark.parametrize('usage_value', [None, -1, True])
async def test_missing_or_invalid_actual_usage_never_refunds_reservation(system, alice, mode, usage_value):
    _, calls = configured(system, alice, mode, usage_value, invalid_tools=False)
    task_id = create(alice, mode)
    await advance(system[0].state, task_id, alice.user_id)
    status, body, usage = snapshot(system, alice, task_id)
    assert status == 'SUCCEEDED' and len(calls) == 1
    entry = body['_planning_usage'][task_id + ':plan:0']
    assert entry['status'] == 'ESTIMATED' and entry['charged'] == entry['reserved']
    assert body['_model_token_charge'] == entry['reserved'] > 0
    if mode == 'cloud':
        assert usage[0]['charged'] == usage[0]['reserved'] == body['_cloud_token_charge']


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['local', 'cloud'])
async def test_post_response_budget_failure_cannot_refund_settled_usage(system, alice, monkeypatch, mode):
    from homeai import agent
    _, calls = configured(system, alice, mode, 51, invalid_tools=False)
    def late_budget_check(*args, **kwargs):
        raise BudgetExceeded('隔离测试：后置预算检查拦截', code='task_tokens_exceeded', scope='task',
                             used=51, requested=1, limit=51, recoverable=False)
    monkeypatch.setattr(agent, 'decode_proposals', late_budget_check)
    task_id = create(alice, mode)
    await advance(system[0].state, task_id, alice.user_id)
    status, body, usage = snapshot(system, alice, task_id)
    assert status == 'WAITING_BUDGET' and len(calls) == 1
    assert body['_model_token_charge'] == 51 and body['_model_rounds'] == 1
    assert body['_planning_usage'][task_id + ':plan:0']['charged'] == 51
    if mode == 'cloud':
        assert body['_cloud_token_charge'] == usage[0]['charged'] == 51
