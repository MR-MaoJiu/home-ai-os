"""公开提醒样例使用真实本地模型与 OPA；业务库隔离，不运行通知投递。"""
import asyncio
import os
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from homeai.config import Settings
from homeai.contracts import ProviderManifest
from homeai.db import Invocation, Provider, Record, Task, scope
from homeai.policy import Policy
from homeai.runtime import run_task


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get('HOMEAI_REMINDER_LIVE') != '1', reason='需要显式启用本地模型公开提醒验收')
async def test_real_model_asks_only_missing_subject_then_schedules_exact_time(system, alice):
    app = system[0].state
    app.policy = Policy(Settings().opa_url)
    endpoint = os.getenv('HOMEAI_REMINDER_MODEL_URL', 'http://127.0.0.1:58100/v1')
    async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
        models = await client.get(endpoint + '/models')
        models.raise_for_status()
        model = models.json()['data'][0]['id']
    manifest = ProviderManifest(id='reminder.public.local', version='1', adapter='openai',
        endpoint=endpoint, model=model, allowed_hosts=['127.0.0.1'], capabilities={'model.generate@v1': 'chat'})
    with system[2]() as db:
        db.add(Provider(id=manifest.id, manifest=manifest.model_dump_json(), enabled=True))
        db.commit()

    response = alice.request('POST', '/api/v1/tasks', {'idempotency_key': str(uuid.uuid4()),
        'message': '明天上午10点提醒我吧', 'timezone': 'Asia/Shanghai', 'max_model_tokens': 262144})
    identifier = response.json()['id']
    async def until(target):
        async with asyncio.timeout(120):
            for _ in range(12):
                await run_task(app, identifier, alice.user_id)
                result = alice.request('GET', '/api/v1/tasks/' + identifier).json()
                if result['status'] in target:
                    return result
            raise AssertionError('公开提醒样例未进入目标状态')
    waiting = await until({'WAITING_CLIENT', 'FAILED', 'SUCCEEDED'})
    assert waiting['status'] == 'WAITING_CLIENT', waiting.get('error')
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, identifier)
        expected = (datetime.fromtimestamp(task.created_at, ZoneInfo('Asia/Shanghai')) + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        body = app.vault.open(task.request, alice.user_id + ':task:' + identifier)
        action_id = body['_client_wait']['action_id']
        assert not list(db.scalars(select(Record).where(Record.kind == 'reminder.item')))
    question = alice.request('GET', '/api/v1/client-actions/' + action_id).json()
    assert not any(phrase in question['purpose'] for phrase in ('几点', '什么时候', '什么时间'))
    answer = alice.request('POST', '/api/v1/client-actions/' + action_id + '/respond', {
        'idempotency_key': str(uuid.uuid4()), 'text': '检查公开测试文档'})
    assert answer.status_code == 200
    done = await until({'SUCCEEDED', 'FAILED', 'WAITING_CLIENT'})
    assert done['status'] == 'SUCCEEDED', done.get('error')
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        reminders = list(db.scalars(select(Record).where(Record.kind == 'reminder.item')))
        assert len(reminders) == 1
        value = app.vault.open(reminders[0].payload, alice.user_id + ':record:' + reminders[0].id)
        assert value['notify_at_due'] is True
        assert datetime.fromisoformat(value['due_at'].replace('Z', '+00:00')) == expected
        calls = list(db.scalars(select(Invocation).where(Invocation.task_id == identifier)))
        assert sum(row.capability == 'reminder.create@v1' and row.status == 'SUCCEEDED' for row in calls) == 1
