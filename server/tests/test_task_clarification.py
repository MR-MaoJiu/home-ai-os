"""临时数据库和固定模型响应验证补充信息；不调用真实模型、手机或用户数据。"""
import json
import uuid

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from conftest import SignedClient
from homeai.api import create_app
from homeai.client_actions import ClientAction, expire_requests
from homeai.contracts import ProviderManifest
from homeai.db import Invocation, Provider, Record, Task, now, scope
from homeai.planner import decode_proposal
from homeai.providers import Registry
from homeai.runtime import run_task


def tool(name, arguments):
    return {'id': str(uuid.uuid4()), 'type': 'function',
        'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}


def proposal(arguments):
    return {'choices': [{'message': {'tool_calls': [tool('ask_user', arguments)]}}]}


def test_explicit_question_maps_to_self_without_new_capability():
    capability, arguments = decode_proposal(proposal({'question': '第二个提醒的内容是什么？'}), allowed_names={'ask_user'})
    assert capability == 'client.request@v1'
    assert arguments == {'target_member_id': 'self', 'kind': 'provide_text',
        'purpose': '第二个提醒的内容是什么？', 'parameters': {'prompt': '第二个提醒的内容是什么？', 'max_length': 4000}}
    options = [{'id': 'one', 'label': '一人份'}, {'id': 'two', 'label': '两人份'}]
    _, arguments = decode_proposal(proposal({'question': '需要几人份？', 'options': options}))
    assert arguments['kind'] == 'choose_option' and arguments['parameters']['options'] == options


@pytest.mark.parametrize('arguments', [
    {'question': ''}, {'question': '   '}, {'question': '请提供支付密码'},
    {'question': '选择登录方式', 'options': [{'id': 'secret', 'label': '输入验证码'}]},
    {'question': '选一种', 'options': [{'id': 'same', 'label': '一'}, {'id': 'same', 'label': '二'}]},
    {'question': '补充内容', 'target_member_id': 'another-member'},
    {'question': '补充内容', 'approved': True},
    {'question': '补充内容', 'options': [{'id': 'one', 'label': '一', 'command': '执行'}]},
])
def test_question_cannot_collect_secrets_or_expand_authority(arguments):
    with pytest.raises(HTTPException):
        decode_proposal(proposal(arguments))


def setup_planner(system, responses):
    app = system[0].state
    manifest = ProviderManifest(id='clarification.local', version='1', adapter='openai',
        endpoint='http://clarification.test/v1', model='fixed-test-model', allowed_hosts=['clarification.test'],
        capabilities={'model.generate@v1': 'chat'})
    with app.db() as db:
        db.add(Provider(id=manifest.id, manifest=manifest.model_dump_json(), enabled=True))
        db.commit()
    calls = []
    def transport(request):
        assert request.url.host == 'clarification.test'
        payload = json.loads(request.content)
        calls.append(payload)
        value = responses.pop(0) if len(responses) > 1 else responses[0]
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', **value}}],
            'usage': {'total_tokens': 8}})
    app.registry = Registry(app.vault, httpx.MockTransport(transport))
    return calls, transport


def submit(alice):
    result = alice.request('POST', '/api/v1/tasks', {'idempotency_key': str(uuid.uuid4()),
        'message': '创建提醒买牛奶，另一个提醒的内容等我补充。', 'max_model_tokens': 262144})
    assert result.status_code == 202, result.text
    return result.json()['id']


def snapshot(system, alice, task_id):
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, task_id)
        body = system[0].state.vault.open(task.request, alice.user_id + ':task:' + task_id)
        return task.status, body


async def wait_for_question(system, alice):
    responses = [{'content': None, 'tool_calls': [tool('create_reminder', {'title': '买牛奶'}),
        tool('ask_user', {'question': '第二个提醒的内容是什么？'}),
        tool('create_reminder', {'title': '模型猜测的标题不能执行'})]},
        {'content': None, 'tool_calls': [tool('create_reminder', {'title': '带雨伞'})]},
        {'content': '两个提醒已创建。'}]
    calls, transport = setup_planner(system, responses)
    task_id = submit(alice)
    for _ in range(5):
        await run_task(system[0].state, task_id, alice.user_id)
        if snapshot(system, alice, task_id)[0] == 'WAITING_CLIENT':
            break
    status, body = snapshot(system, alice, task_id)
    assert status == 'WAITING_CLIENT'
    assert len(body['steps']) == 2
    assert len(calls) == 1
    return task_id, body['_client_wait']['action_id'], calls, transport


async def test_question_waits_then_restarts_original_task_without_repeating_effects(system, alice):
    task_id, action_id, calls, transport = await wait_for_question(system, alice)
    detail = alice.request('GET', '/api/v1/client-actions/' + action_id).json()
    assert detail['kind'] == 'provide_text' and detail['purpose'] == '第二个提醒的内容是什么？'
    assert detail['task_id'] == task_id and detail['can_respond']
    assert detail['expires_at'] - detail['created_at'] > 23 * 3600
    await run_task(system[0].state, task_id, alice.user_id)
    assert len(calls) == 1
    other = SignedClient(system[1], system[2])
    response = {'idempotency_key': str(uuid.uuid4()), 'text': '带雨伞'}
    assert other.request('POST', '/api/v1/client-actions/' + action_id + '/respond', response).status_code == 404
    accepted = alice.request('POST', '/api/v1/client-actions/' + action_id + '/respond', response)
    assert accepted.status_code == 200, accepted.text
    assert alice.request('POST', '/api/v1/client-actions/' + action_id + '/respond', response).json() == accepted.json()
    assert snapshot(system, alice, task_id)[0] == 'RECEIVED'

    # 全新 App 对象只复用持久化库与密钥，不依赖之前规划器或待办内存。
    previous = system[0].state
    resumed = create_app(previous.settings, previous.vault, system[2], previous.policy).state
    resumed.registry = Registry(resumed.vault, httpx.MockTransport(transport))
    for _ in range(6):
        await run_task(resumed, task_id, alice.user_id)
        if snapshot(system, alice, task_id)[0] == 'SUCCEEDED':
            break
    assert snapshot(system, alice, task_id)[0] == 'SUCCEEDED'
    returned_tools = [item for item in calls[1]['messages'] if item['role'] == 'tool']
    assert any('带雨伞' in item['content'] for item in returned_tools)
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        rows = list(db.scalars(select(Invocation).where(Invocation.task_id == task_id).order_by(Invocation.step)))
        assert [row.capability for row in rows] == ['reminder.create@v1', 'client.request@v1', 'reminder.create@v1']
        assert all(row.status == 'SUCCEEDED' for row in rows)
        reminders = list(db.scalars(select(Record).where(Record.kind == 'reminder.item')))
        titles = [previous.vault.open(row.payload, alice.user_id + ':record:' + row.id)['title'] for row in reminders]
        assert sorted(titles) == ['买牛奶', '带雨伞']
    await run_task(resumed, task_id, alice.user_id)
    assert len(calls) == 4


@pytest.mark.parametrize('ending', ['deny', 'cancel', 'expire'])
async def test_question_denial_cancel_or_expiry_cannot_resume_or_repeat(system, alice, ending):
    task_id, action_id, calls, _ = await wait_for_question(system, alice)
    if ending == 'deny':
        response = {'idempotency_key': str(uuid.uuid4()), 'reason': '暂不补充'}
        first = alice.request('POST', '/api/v1/client-actions/' + action_id + '/deny', response)
        assert first.status_code == 200
        assert alice.request('POST', '/api/v1/client-actions/' + action_id + '/deny', response).json() == first.json()
    elif ending == 'cancel':
        assert alice.request('POST', '/api/v1/tasks/' + task_id + '/cancel').status_code == 200
    else:
        with system[2]() as db:
            db.get(ClientAction, action_id).expires_at = now() - 1
            db.commit()
        expire_requests(system[0].state)
    assert alice.request('POST', '/api/v1/client-actions/' + action_id + '/respond',
        {'idempotency_key': str(uuid.uuid4()), 'text': '迟到的回复'}).status_code == 409
    await run_task(system[0].state, task_id, alice.user_id)
    assert len(calls) == 1


async def test_ordinary_text_with_question_is_not_guessed_into_client_wait(system, alice):
    setup_planner(system, [{'content': '“你好吗？”是一个日常问候的例子。'}])
    result = alice.request('POST', '/api/v1/tasks', {'idempotency_key': str(uuid.uuid4()),
        'message': '举一个日常问候的例子', 'max_model_tokens': 262144})
    task_id = result.json()['id']
    await run_task(system[0].state, task_id, alice.user_id)
    assert snapshot(system, alice, task_id)[0] == 'SUCCEEDED'
    with system[2]() as db:
        assert not list(db.scalars(select(ClientAction).where(ClientAction.task_id == task_id)))
