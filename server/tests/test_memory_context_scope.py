"""隔离回归：历史追问不批量读取长期记忆，显式检索仍保留来源和披露检查。"""
import json

import httpx
import pytest
from sqlalchemy import select

from homeai.db import Invocation, Task, scope
from homeai.runtime import run_task
from test_conversations import finish, new_chat, send
from test_planning_resumption import setup_models
from test_security_data import put, record


def memory(alice, content, *, sensitive=False):
    value = record(source_id=content, kind='memory.fact', payload={'content': content})
    value.update(sensitivity='SENSITIVE' if sensitive else 'PRIVATE',
                 cloud_policy='LOCAL_ONLY' if sensitive else 'REDACT_AND_ALLOW')
    return put(alice, value)


def task_body(system, alice, task_id):
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, task_id)
        return task.status, system[0].state.vault.open(task.request, alice.user_id + ':task:' + task.id)


@pytest.mark.asyncio
@pytest.mark.parametrize('question', [
    '还记得刚才的代号是什么吗？',
    '我喜欢这个公开问题的回答，再解释一下。',
    '前面说的我的习惯是什么意思？',
])
async def test_ordinary_followup_does_not_load_unrelated_sensitive_memories(system, alice, question):
    _, _, traffic = setup_models(system, alice)
    private_id = memory(alice, '隔离敏感记忆：健康测试标记ZH-731', sensitive=True)
    conversation = new_chat(alice)
    first = send(alice, conversation, '这轮公开讨论的代号是青竹42。').json()
    finish(system, alice, first['task_id'], '公开讨论代号青竹42。')
    task_id = send(alice, conversation, question).json()['task_id']

    await run_task(system[0].state, task_id, alice.user_id)

    status, body = task_body(system, alice, task_id)
    assert status == 'SUCCEEDED'
    assert private_id not in body.get('_record_dependencies', {})
    assert not body.get('_client_wait') and not body.get('_cloud_consents')
    outgoing = [payload for host, payload in traffic if host == 'model.example']
    assert outgoing
    encoded = json.dumps(outgoing, ensure_ascii=False)
    assert 'ZH-731' not in encoded and private_id not in encoded
    assert '青竹42' in encoded
    assert all('search_memories' in {tool['function']['name'] for tool in payload['tools']}
               for payload in outgoing)


@pytest.mark.asyncio
@pytest.mark.parametrize('sensitive', [False, True])
async def test_explicit_memory_tool_keeps_current_task_sources_and_consent(system, alice, sensitive):
    setup_models(system, alice)
    target_id = memory(alice, '长期偏好：哑光纸', sensitive=sensitive)
    other_id = memory(alice, '无关敏感记忆标记ZH-913', sensitive=True)
    registry = system[0].state.registry
    original = registry.transport
    outgoing = []

    def transport(request):
        if request.url.host != 'model.example':
            return original.handle_request(request)
        payload = json.loads(request.content)
        outgoing.append(payload)
        if len(outgoing) == 1:
            names = {tool['function']['name'] for tool in payload['tools']}
            assert 'search_memories' in names
            message = {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': 'specific-memory-query', 'type': 'function', 'function': {
                    'name': 'search_memories', 'arguments': json.dumps({'query': '哑光纸'})}}]}
        else:
            message = {'role': 'assistant', 'content': '检索到关于哑光纸的偏好。'}
        return httpx.Response(200, json={'choices': [{'message': message}], 'usage': {'total_tokens': 8}})

    registry.transport = httpx.MockTransport(transport)
    task_id = send(alice, new_chat(alice), '请检索长期记忆中关于哑光纸的偏好。').json()['task_id']
    for _ in range(4):
        await run_task(system[0].state, task_id, alice.user_id)
        status, body = task_body(system, alice, task_id)
        if status in {'SUCCEEDED', 'WAITING_CLIENT', 'FAILED'}:
            break

    assert status == ('WAITING_CLIENT' if sensitive else 'SUCCEEDED')
    assert body['_record_dependencies'] == {target_id: 1}
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        calls = list(db.scalars(select(Invocation).where(Invocation.task_id == task_id)))
        assert len(calls) == 1
        assert calls[0].capability == 'memory.search@v1' and calls[0].status == 'SUCCEEDED'
    encoded = json.dumps(outgoing, ensure_ascii=False)
    assert other_id not in encoded and 'ZH-913' not in encoded
    if sensitive:
        assert len(outgoing) == 1
        assert target_id not in encoded
        actions = alice.request('GET', '/api/v1/client-actions').json()['items']
        assert len(actions) == 1 and actions[0]['kind'] == 'cloud.disclose'
        assert actions[0]['parameters']['record_versions'] == {target_id: 1}
    else:
        assert len(outgoing) == 2 and target_id in encoded
        assert not body.get('_client_wait')


@pytest.mark.asyncio
async def test_current_ordinary_preference_still_saves_without_loading_other_memories(system, alice):
    _, _, traffic = setup_models(system, alice)
    private_id = memory(alice, '无关敏感记忆标记ZH-511', sensitive=True)
    task_id = send(alice, new_chat(alice), '我喜欢喝茶。').json()['task_id']

    await run_task(system[0].state, task_id, alice.user_id)

    status, body = task_body(system, alice, task_id)
    assert status == 'SUCCEEDED' and body['_memory_observation']['status'] == 'SAVED'
    assert private_id not in body.get('_record_dependencies', {})
    assert not any(host == 'model.example' for host, _ in traffic)
    assert 'ZH-511' not in json.dumps(traffic, ensure_ascii=False)
