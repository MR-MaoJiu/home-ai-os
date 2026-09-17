"""真实 PostgreSQL/OPA 验证工作流，外部发送动作在审批前停止，不伪造发送结果。"""
import hashlib
import os
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func, text
from homeai.api import create_app
from homeai.config import Settings
from homeai.db import Task, Record, Invocation, Approval, scope
from homeai.runtime import run_task
from conftest import SignedClient

pytestmark = pytest.mark.skipif(os.environ.get('HOMEAI_INTEGRATION') != '1', reason='需要真实 PostgreSQL 和 OPA')


@pytest.fixture
def workflow():
    settings = Settings()
    settings.database_url = settings.database_url.rsplit('/', 1)[0] + '/homeai_test'
    app = create_app(settings)
    client = TestClient(app)
    user = SignedClient(client, app.state.db, household=str(uuid.uuid4()))
    try:
        yield app.state, user
    finally:
        client.close()
        # 每个隔离应用拥有独立连接池，测试结束必须主动释放。
        app.state.db.kw['bind'].dispose()


def create(user, steps):
    response = user.request('POST', '/api/v1/tasks', {'idempotency_key': str(uuid.uuid4()), 'steps': steps})
    assert response.status_code == 202, response.text
    return response.json()['id']


@pytest.mark.asyncio
async def test_persistent_steps_references_and_lock(workflow):
    app, user = workflow
    tid = create(user, [
        {'capability': 'reminder.create@v1', 'arguments': {'title': '第一步'}},
        {'capability': 'reminder.create@v1', 'arguments': {'title': '第二步', 'linked_record': {'$step': 0, 'path': ['record_id']}}},
    ])
    await run_task(app, tid, user.user_id)
    assert user.request('GET', '/api/v1/tasks/' + tid).json()['status'] == 'RECEIVED'
    first = user.request('GET', '/api/v1/tasks/' + tid + '/steps').json()[0]
    with app.db() as db:
        engine = db.get_bind()
    key = int.from_bytes(hashlib.sha256(('task:' + tid).encode()).digest()[:8], 'big', signed=True)
    with engine.connect() as lock:
        lock.execute(text('SELECT pg_advisory_lock(:key)'), {'key': key})
        lock.commit()
        try:
            await run_task(app, tid, user.user_id)
            assert len(user.request('GET', '/api/v1/tasks/' + tid + '/steps').json()) == 1
        finally:
            lock.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': key})
            lock.commit()
    # 模拟进程在步骤边界退出：持久化状态由真实数据库恢复，不替换模型或工具。
    with app.db() as db:
        from homeai.db import Principal
        principal = db.get(Principal, user.user_id)
        scope(db, user.user_id, principal.household_id)
        db.get(Task, tid).status = 'EXECUTING'
        db.commit()
    await run_task(app, tid, user.user_id)
    await run_task(app, tid, user.user_id)
    assert user.request('GET', '/api/v1/tasks/' + tid).json()['status'] == 'SUCCEEDED'
    second = user.request('GET', '/api/v1/tasks/' + tid + '/steps').json()[1]
    record = user.request('GET', '/api/v1/data/' + second['result']['record_id']).json()
    assert record['payload']['linked_record'] == first['result']['record_id']
    assert len(user.request('GET', '/api/v1/tasks/' + tid + '/steps').json()) == 2


@pytest.mark.asyncio
async def test_workflow_approval_and_cancellation(workflow):
    app, user = workflow
    tid = create(user, [
        {'capability': 'reminder.create@v1', 'arguments': {'title': '已完成前序'}},
        {'capability': 'mail.send@v1', 'arguments': {'to': 'test@example.com', 'text': '不执行发送'}},
        {'capability': 'reminder.create@v1', 'arguments': {'title': '不应执行'}},
    ])
    await run_task(app, tid, user.user_id)
    await run_task(app, tid, user.user_id)
    assert user.request('GET', '/api/v1/tasks/' + tid).json()['status'] == 'AWAITING_APPROVAL'
    approval = user.request('GET', '/api/v1/approvals').json()[0]
    assert user.request('POST', '/api/v1/tasks/' + tid + '/cancel').status_code == 200
    assert user.request('POST', '/api/v1/approvals/' + approval['id'], {'decision': 'APPROVED'}).status_code == 409
    await run_task(app, tid, user.user_id)
    steps = user.request('GET', '/api/v1/tasks/' + tid + '/steps').json()
    assert len(steps) == 2 and steps[0]['status'] == 'SUCCEEDED'
    assert user.request('GET', '/api/v1/tasks/' + tid).json()['status'] == 'CANCELED'


def test_workflow_budget_and_private_projection_rejected(workflow):
    _, user = workflow
    body = {'idempotency_key': str(uuid.uuid4()), 'max_steps': 1, 'steps': [{'capability': 'reminder.create@v1'}, {'capability': 'reminder.create@v1'}]}
    assert user.request('POST', '/api/v1/tasks', body).status_code == 422
    body['steps'] = [{'capability': 'memory.graph.purge@v1'}]
    assert user.request('POST', '/api/v1/tasks', body).status_code == 403


@pytest.mark.asyncio
async def test_unknown_effect_reconciliation_does_not_send(workflow):
    app, user = workflow
    tid = create(user, [{'capability': 'mail.send@v1', 'arguments': {'to': 'test@example.com', 'text': '恢复测试不发送'}}])
    await run_task(app, tid, user.user_id)
    approval = user.request('GET', '/api/v1/approvals').json()[0]
    assert user.request('POST', '/api/v1/approvals/' + approval['id'], {'decision': 'APPROVED'}).status_code == 200
    with app.db() as db:
        from homeai.db import Principal, now
        principal = db.get(Principal, user.user_id)
        scope(db, user.user_id, principal.household_id)
        task = db.get(Task, tid)
        task.status, task.deadline = 'EXECUTING', now() - 1
        invocation = db.scalar(select(Invocation).where(Invocation.task_id == tid))
        invocation.status = 'EXECUTING'
        db.commit()
    # 即使任务已过期，也必须保留外部结果不明，不能误报确定失败。
    await run_task(app, tid, user.user_id)
    assert user.request('GET', '/api/v1/tasks/' + tid).json()['status'] == 'NEEDS_RECONCILIATION'
    body = {'decision': 'NOT_EXECUTED', 'evidence': '管理员已检查外部系统，没有执行记录。'}
    assert user.request('POST', '/api/v1/tasks/' + tid + '/reconcile', body).status_code == 409
    body['decision'] = 'ABORT'
    assert user.request('POST', '/api/v1/tasks/' + tid + '/reconcile', body).json()['status'] == 'FAILED'
    assert user.request('POST', '/api/v1/tasks/' + tid + '/reconcile', body).status_code == 409


@pytest.mark.asyncio
async def test_read_retry_budget_against_unreachable_service(workflow):
    import socket
    from homeai.contracts import ProviderManifest
    from homeai.db import Provider, Principal
    app, user = workflow
    # 真实 TCP 连接失败，不注入成功响应或伪造 Provider 结果。
    with socket.socket() as socket_probe:
        socket_probe.bind(('127.0.0.1', 0))
        port = socket_probe.getsockname()[1]
    provider_id = 'a.workflow.' + uuid.uuid4().hex
    manifest = ProviderManifest(id=provider_id, version='1', adapter='homeassistant', endpoint=f'http://127.0.0.1:{port}', allowed_hosts=['127.0.0.1'], capabilities={'home.states@v1': 'states'})
    with app.db() as db:
        db.add(Provider(id=provider_id, manifest=manifest.model_dump_json(), enabled=True))
        db.commit()
    try:
        tid = create(user, [{'capability': 'home.states@v1'}])
        await run_task(app, tid, user.user_id)
        assert user.request('GET', '/api/v1/tasks/' + tid).json()['status'] == 'RECEIVED'
        await run_task(app, tid, user.user_id)
        assert user.request('GET', '/api/v1/tasks/' + tid).json()['status'] == 'FAILED'
        with app.db() as db:
            principal = db.get(Principal, user.user_id)
            scope(db, user.user_id, principal.household_id)
            task = db.get(Task, tid)
            payload = app.vault.open(task.request, user.user_id + ':task:' + tid)
            assert payload['_attempts']['0'] == 2
    finally:
        with app.db() as db:
            db.get(Provider, provider_id).enabled = False
            db.commit()


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get('HOMEAI_MODEL_TEST') != '1', reason='需要真实 llama.cpp 生成模型')
async def test_workflow_passes_actual_tool_result_to_local_model(workflow):
    from homeai.contracts import ProviderManifest
    from homeai.db import Provider, Principal
    app, user = workflow
    provider_id = 'a.workflow.model'
    manifest = ProviderManifest(id=provider_id, version='8460', adapter='openai', endpoint='http://127.0.0.1:58080/v1', model='Qwen3-0.6B-Q8_0.gguf', allowed_hosts=['127.0.0.1'], capabilities={'model.generate@v1': 'chat'})
    with app.db() as db:
        provider = db.get(Provider, provider_id)
        if provider:
            provider.manifest, provider.enabled = manifest.model_dump_json(), True
        else:
            db.add(Provider(id=provider_id, manifest=manifest.model_dump_json(), enabled=True))
        db.commit()
    try:
        tid = create(user, [
            {'capability': 'reminder.create@v1', 'arguments': {'title': '准备资料'}},
            {'capability': 'model.generate@v1', 'arguments': {'message': '用一句中文解释工具结果的 stored 和 device_sync pending。/no_think', 'context': {'$step': 0}}},
        ])
        await run_task(app, tid, user.user_id)
        await run_task(app, tid, user.user_id)
        result = user.request('GET', '/api/v1/tasks/' + tid).json()
        assert result['status'] == 'SUCCEEDED', result
        assert result['result']['usage']['completion_tokens'] > 0
        assert result['result']['choices'][0]['message']['content']
        steps = user.request('GET', '/api/v1/tasks/' + tid + '/steps').json()
        with app.db() as db:
            principal = db.get(Principal, user.user_id)
            scope(db, user.user_id, principal.household_id)
            invocation = db.get(Invocation, steps[1]['id'])
            arguments = app.vault.open(invocation.arguments, user.user_id + ':invocation:' + invocation.id)
            assert steps[0]['result']['record_id'] in arguments['messages'][1]['content']
    finally:
        with app.db() as db:
            db.get(Provider, provider_id).enabled = False
            db.commit()


@pytest.mark.asyncio
async def test_expired_approval_is_finalized_by_worker(workflow):
    from homeai.worker import cycle
    from homeai.db import Principal, now
    app, user = workflow
    tid = create(user, [{'capability': 'mail.send@v1', 'arguments': {'to': 'test@example.com'}}])
    await run_task(app, tid, user.user_id)
    with app.db() as db:
        principal = db.get(Principal, user.user_id)
        scope(db, user.user_id, principal.household_id)
        invocation = db.scalar(select(Invocation).where(Invocation.task_id == tid))
        approval = db.scalar(select(Approval).where(Approval.invocation_id == invocation.id))
        approval.expires_at = now() - 1
        db.commit()
    await cycle(app)
    result = user.request('GET', '/api/v1/tasks/' + tid).json()
    assert result['status'] == 'FAILED' and '过期' in result['error']
