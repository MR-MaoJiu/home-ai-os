"""模型响应契约与持久化回归；只使用临时数据库和模型HTTP替身。"""
import copy
import json
import uuid

import httpx
import pytest
from sqlalchemy import func, select

from homeai.db import Invocation, Record, Task, scope
from homeai.model_routing import ModelUsage
from homeai.model_responses import ModelResponseError, assistant_message, diagnostics, normalize_response
from homeai.providers import Registry
from homeai.runtime import run_task
from homeai.source_fidelity import clean_answer
from test_planning_resumption import setup_models


REASONING = '仅用于隔离测试的内部思考哨兵'
REFUSAL = '仅用于隔离测试的拒答正文哨兵'


def response(content=None, *, finish='stop', reasoning=None, refusal=None, tools=None):
    message = {'role': 'assistant', 'content': content}
    if reasoning is not None:
        message['reasoning_content'] = reasoning
    if refusal is not None:
        message['refusal'] = refusal
    if tools is not None:
        message['tool_calls'] = tools
    return {'choices': [{'message': message, 'finish_reason': finish}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 7, 'total_tokens': 17}}


def call(name='list_automations', arguments=None):
    return {'id': 'public-tool-call', 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments or {})}}


@pytest.mark.parametrize('value', [
    {}, {'choices': []}, {'choices': None}, {'choices': {}}, {'choices': [None]},
    {'choices': [{'message': None}]}, response(None), response(''), response(' \n\t '),
])
def test_missing_or_empty_visible_response_is_not_a_success(value):
    with pytest.raises(ModelResponseError) as failure:
        normalize_response(value)
    assert failure.value.code and isinstance(failure.value.diagnostics, dict)
    assert REASONING not in str(failure.value.detail)


@pytest.mark.parametrize('value', [
    response(None, reasoning=REASONING), response('', reasoning=REASONING),
    response('<think>' + REASONING + '</think>'), response('<think>' + REASONING),
    response('<think>第一段</think>\n<think>' + REASONING + '</think>'),
])
def test_reasoning_without_final_answer_is_explicit_and_never_echoed(value):
    with pytest.raises(ModelResponseError) as failure:
        normalize_response(value)
    assert failure.value.code == 'reasoning_only'
    assert REASONING not in str(failure.value.detail)
    assert REASONING not in json.dumps(failure.value.diagnostics, ensure_ascii=False)


@pytest.mark.parametrize('content,tools', [('', None), ('部分公开答复', None), (None, [call('create_reminder', {'title': '不能执行的截断计划'})])])
def test_length_finish_reason_is_not_misreported_as_complete_or_executable(content, tools):
    with pytest.raises(ModelResponseError) as failure:
        normalize_response(response(content, finish='length', reasoning=REASONING, tools=tools))
    assert failure.value.code == 'output_limit'
    assert failure.value.diagnostics['finish_reason'] == 'length'
    assert REASONING not in json.dumps(failure.value.diagnostics, ensure_ascii=False)


@pytest.mark.parametrize('value', [response('部分公开答复', finish='content_filter'), response(None, refusal=REFUSAL)])
def test_filter_and_refusal_fail_with_non_body_diagnostics(value):
    with pytest.raises(ModelResponseError) as failure:
        normalize_response(value)
    assert failure.value.code
    assert REFUSAL not in str(failure.value.detail)
    assert REFUSAL not in json.dumps(failure.value.diagnostics, ensure_ascii=False)
    if value['choices'][0]['message'].get('refusal'):
        assert failure.value.diagnostics['has_refusal'] is True
    else:
        assert failure.value.diagnostics['finish_reason'] == 'content_filter'


def test_structured_text_blocks_are_supported_and_reasoning_field_is_removed():
    value = response([{'type': 'text', 'text': '第一段公开正文'}, {'type': 'output_text', 'text': '第二段公开正文'}], reasoning=REASONING)
    message = assistant_message(value)
    assert isinstance(message['content'], str)
    assert message['content'].index('第一段公开正文') < message['content'].index('第二段公开正文')
    assert 'reasoning_content' not in message
    normalized = normalize_response(value)
    assert normalized['choices'][0]['message']['content'] == message['content']
    assert REASONING not in json.dumps(normalized, ensure_ascii=False)


def test_non_text_structured_blocks_cannot_become_a_stringified_answer():
    with pytest.raises(ModelResponseError):
        normalize_response(response([{'type': 'image_url', 'image_url': {'url': 'https://example.invalid/not-loaded.png'}}]))


def test_legitimate_tool_calls_do_not_require_content_but_need_round_permission():
    value = response(None, finish='tool_calls', reasoning=REASONING, tools=[call()])
    normalized = normalize_response(value)
    message = normalized['choices'][0]['message']
    assert message['content'] == '' and message['tool_calls'][0]['function']['name'] == 'list_automations'
    assert 'reasoning_content' not in message
    with pytest.raises(ModelResponseError):
        normalize_response(value, allow_tools=False)


def test_diagnostics_expose_only_counts_and_protocol_categories():
    value = response('公开正文', reasoning=REASONING, refusal=REFUSAL)
    value['usage']['provider_private_field'] = REASONING
    result = diagnostics(value)
    assert result['content_characters'] == len('公开正文')
    assert result['reasoning_characters'] == len(REASONING)
    assert result['tool_call_count'] == 0 and result['has_refusal'] is True
    assert result['finish_reason'] == 'stop'
    encoded = json.dumps(result, ensure_ascii=False)
    assert REASONING not in encoded and REFUSAL not in encoded and '公开正文' not in encoded


def test_cleaning_repeated_think_prefixes_or_unclosed_think_cannot_leak_reasoning():
    assert clean_answer('<think>第一段</think>\n<think>' + REASONING + '</think>\n</think>\n可见答复') == '可见答复'
    assert clean_answer('<think>' + REASONING) == ''
    assert clean_answer('<think>' + REASONING + '</think>') == ''


def configure(system, user, mode, answer):
    setup_models(system, user)
    if mode == 'local':
        config = user.request('GET', '/api/v1/manage/models').json()['configuration']
        result = user.request('PUT', '/api/v1/manage/models', {**config, 'cloud_planner': None, 'vision': None, 'shared_cloud': False})
        assert result.status_code == 200, result.text
    seen = []
    def transport(request):
        payload = json.loads(request.content)
        if request.url.host == 'privacy.test':
            review = '"safe"' in payload['messages'][0]['content']
            content = {'safe': True, 'uncertain': False} if review else {'entities': [], 'sensitive': False, 'uncertain': False}
            return httpx.Response(200, json=response(json.dumps(content)))
        assert request.url.host == ('model.example' if mode == 'cloud' else 'fast.test')
        seen.append(payload)
        return httpx.Response(200, json=copy.deepcopy(answer))
    app = system[0].state
    app.registry = Registry(app.vault, httpx.MockTransport(transport))
    return seen


def create(user, mode, *, explicit=False):
    body = {'mode': mode, 'message': '请简短说明公开队列知识。', 'idempotency_key': str(uuid.uuid4()), 'max_model_tokens': 262144}
    if explicit:
        body.update(capability='model.generate@v1', arguments={'messages': [{'role': 'user', 'content': '请回复公开说明。'}], 'max_tokens': 128})
    result = user.request('POST', '/api/v1/tasks', body)
    assert result.status_code == 202, result.text
    return result.json()['id']


def state(system, user, identifier):
    with system[2]() as db:
        scope(db, user.user_id, 'h1')
        task = db.get(Task, identifier)
        body = system[0].state.vault.open(task.request, user.user_id + ':task:' + identifier)
        result = system[0].state.vault.open(task.result, user.user_id + ':task-result:' + identifier) if task.result else None
        charged = list(db.scalars(select(ModelUsage.charged).where(ModelUsage.task_id == identifier)))
        return task.status, task.error, body, result, charged


FAILURES = [
    {'choices': [], 'usage': {'total_tokens': 17}},
    response(None, reasoning=REASONING),
    response('<think>' + REASONING + '</think>'),
    response('<think>' + REASONING),
    response('部分公开答复', finish='length', tools=[call('create_reminder', {'title': '不能执行的截断计划'})]),
    response(None, finish='content_filter'),
    response(None, refusal=REFUSAL),
]


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['local', 'cloud'])
@pytest.mark.parametrize('answer', FAILURES)
async def test_agent_never_succeeds_blank_or_runs_incomplete_tools_and_keeps_usage(system, alice, mode, answer):
    seen = configure(system, alice, mode, answer)
    task_id = create(alice, mode)
    await run_task(system[0].state, task_id, alice.user_id)
    status, error, body, result, charged = state(system, alice, task_id)
    assert status == 'FAILED' and error and result is None
    assert REASONING not in error and REFUSAL not in error
    assert body['_model_token_charge'] == 17
    report = body['_model_response_diagnostics'][-1]
    assert report['code'] != 'valid_response' and report['total_tokens'] == 17
    assert REASONING not in json.dumps(report, ensure_ascii=False)
    assert REFUSAL not in json.dumps(report, ensure_ascii=False)
    if mode == 'cloud':
        assert charged == [17] and body['_cloud_token_charge'] == 17
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        assert db.scalar(select(func.count()).select_from(Invocation).where(Invocation.task_id == task_id)) == 0
        assert db.scalar(select(func.count()).select_from(Record).where(Record.kind == 'reminder.item')) == 0
    await run_task(system[0].state, task_id, alice.user_id)
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['local', 'cloud'])
async def test_agent_accepts_structured_visible_text_and_keeps_only_final_answer(system, alice, mode):
    answer = response([{'type': 'text', 'text': '公开第一段'}, {'type': 'output_text', 'text': '公开第二段'}], reasoning=REASONING)
    seen = configure(system, alice, mode, answer)
    task_id = create(alice, mode)
    await run_task(system[0].state, task_id, alice.user_id)
    status, _, body, result, _ = state(system, alice, task_id)
    assert status == 'SUCCEEDED' and len(seen) == 1
    visible = result['choices'][0]['message']['content']
    assert '公开第一段' in visible and '公开第二段' in visible
    assert REASONING not in json.dumps(result, ensure_ascii=False)
    assert body['_model_token_charge'] == 17


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['local', 'cloud'])
async def test_agent_can_plan_a_legitimate_tool_without_visible_content(system, alice, mode):
    answer = response(None, finish='tool_calls', reasoning=REASONING, tools=[call()])
    seen = configure(system, alice, mode, answer)
    task_id = create(alice, mode)
    await run_task(system[0].state, task_id, alice.user_id)
    status, error, body, result, _ = state(system, alice, task_id)
    assert status == 'RECEIVED' and error is None and result is None
    assert body['steps'] == [{'capability': 'automation.list@v1', 'arguments': {}}]
    assert body['_model_token_charge'] == 17 and len(seen) == 1
    assert REASONING not in json.dumps(body['_agent_batches'], ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['local', 'cloud'])
@pytest.mark.parametrize('answer', [FAILURES[0], FAILURES[2], response([{'type': 'text', 'text': '公开显式调用结果'}])])
async def test_explicit_model_runtime_applies_same_visible_response_contract(system, alice, mode, answer):
    seen = configure(system, alice, mode, answer)
    task_id = create(alice, mode, explicit=True)
    await run_task(system[0].state, task_id, alice.user_id)
    status, error, body, result, charged = state(system, alice, task_id)
    expected_success = isinstance(answer.get('choices', [{}])[0].get('message', {}).get('content'), list) if answer.get('choices') else False
    if expected_success:
        assert status == 'SUCCEEDED'
        assert result['choices'][0]['message']['content'] == '公开显式调用结果'
    else:
        assert status == 'FAILED' and result is None and error
        assert REASONING not in error
    if mode == 'cloud':
        assert charged == [17] and body['_cloud_token_charge'] == 17
    report = body['_model_response_diagnostics'][-1]
    assert report['total_tokens'] == 17
    assert REASONING not in json.dumps(report, ensure_ascii=False)
    await run_task(system[0].state, task_id, alice.user_id)
    assert len(seen) == 1
