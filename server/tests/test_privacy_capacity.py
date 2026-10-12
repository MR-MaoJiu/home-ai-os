"""容量恢复完整覆盖原文，真实适配器错误结构不被误报为缺少资料。"""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from homeai import cloud_gateway, privacy_batches
from homeai.cloud_gateway import PrivacyUnavailable, _text_checked, local_check
from homeai.contracts import ProviderManifest
from homeai.providers import Registry


def success(entities=(), *, review=False, sensitive=False, reasons=(), **fields):
    result = ({'safe': True, 'uncertain': False} if review else
              {'entities': list(entities), 'sensitive': sensitive, 'uncertain': False})
    result.update(reason_codes=list(reasons), **fields)
    return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(result)}}]})


def capacity(status=400):
    return httpx.Response(status, json={'error': {'type': 'exceed_context_size_error',
        'message': '此处可能包含私人原文，不得回显', 'n_prompt_tokens': 14274, 'n_ctx': 8192}})


def local_app(monkeypatch, respond, *, timeout=30):
    manifest = ProviderManifest(id='local.privacy', version='test', adapter='openai',
        endpoint='http://privacy.invalid/v1', model='isolated', allowed_hosts=['privacy.invalid'],
        capabilities={'model.generate@v1': 'chat'})
    # 低于正常配置下限的超时仅用于验证整轮截止时间，不更改实际契约。
    manifest = manifest.model_copy(update={'timeout_seconds': timeout})
    monkeypatch.setattr(cloud_gateway, 'resolve_role', lambda *args: manifest)
    async def allow(*args):
        pass
    return SimpleNamespace(policy=SimpleNamespace(check=allow),
        registry=Registry(None, httpx.MockTransport(respond)))


@pytest.mark.asyncio
@pytest.mark.parametrize('review', [False, True])
async def test_short_success_keeps_single_request(monkeypatch, review):
    calls = []
    def respond(request):
        calls.append(json.loads(request.content))
        return success(review=review)
    app = local_app(monkeypatch, respond)
    result = await local_check(app, None, None, '公开短内容', review=review)
    assert len(calls) == 1 and calls[0]['messages'][1]['content'] == '公开短内容'
    assert calls[0]['response_format']['type'] == 'json_schema'
    assert result['uncertain'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [400, 413, 422])
async def test_capacity_split_covers_all_unicode_and_boundary_entities(monkeypatch, status):
    original = ''.join(chr(0x4e00 + index) for index in range(10000))
    entities = [original[:8], original[4700:5200], original[-10:]]
    calls, leaves = [], []
    def respond(request):
        chunk = json.loads(request.content)['messages'][1]['content']
        calls.append(chunk)
        if len(chunk) > 4000:
            return capacity(status)
        leaves.append(chunk)
        found = [item for item in entities if item in chunk]
        return success(found, sensitive=entities[-1] in chunk,
            reasons=['health'] if entities[-1] in chunk else ['name'] if found else [])
    result = await local_check(local_app(monkeypatch, respond), None, None, original)
    assert calls[0] == original and len(calls) == 7
    assert result['entities'] == entities and result['sensitive'] is True
    assert set(result['reason_codes']) == {'name', 'health'}
    assert all(any(char in chunk for chunk in leaves) for char in original)
    assert all(any(entity in chunk for chunk in leaves) for entity in entities)
    assert all(original[original.index(chunk):original.index(chunk) + len(chunk)] == chunk for chunk in leaves)
    assert all(len(set(left) & set(right)) >= 500 for left, right in zip(leaves, leaves[1:]))


@pytest.mark.asyncio
@pytest.mark.parametrize(('status', 'body', 'code'), [
    (400, {'error': {'type': 'invalid_request_error', 'message': 'exceed_context_size_error 原文'}}, 'privacy_request_rejected'),
    (400, {'error': 'request rejected'}, 'privacy_request_rejected'),
    (400, {'error': {'code': 'unsupported_json_schema'}}, 'privacy_format_unsupported'),
    (401, {}, 'privacy_auth_failed'),
    (403, {}, 'privacy_auth_failed'),
    (404, {}, 'privacy_endpoint_missing'),
    (429, {}, 'privacy_busy'),
    (503, {}, 'privacy_service_error'),
])
async def test_non_capacity_http_errors_never_retry_or_echo_body(monkeypatch, status, body, code):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status, json=body)
    with pytest.raises(PrivacyUnavailable) as caught:
        await local_check(local_app(monkeypatch, respond), None, None, '公开内容' * 2000)
    assert len(calls) == 1 and caught.value.code == code
    assert '原文' not in caught.value.detail and 'privacy.invalid' not in caught.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['uncertain', 'schema', 'entity_from_other_chunk', 'timeout', 'http'])
async def test_any_detection_leaf_failure_prevents_partial_success(monkeypatch, failure):
    calls = []
    original = '甲' * 1800 + '乙' * 1800
    def respond(request):
        chunk = json.loads(request.content)['messages'][1]['content']
        calls.append(chunk)
        if len(calls) == 1:
            return capacity()
        if len(calls) == 2:
            return success(['甲' * 5])
        if failure == 'uncertain':
            return success(uncertain=True)
        if failure == 'schema':
            return success(sensitive='invalid')
        if failure == 'entity_from_other_chunk':
            return success(['甲' * 500])
        if failure == 'timeout':
            raise httpx.ReadTimeout('不得回显原文')
        return httpx.Response(500, text='不得回显原文')
    with pytest.raises(PrivacyUnavailable) as caught:
        await local_check(local_app(monkeypatch, respond), None, None, original)
    assert len(calls) == 3
    assert caught.value.code == {'uncertain': 'uncertain', 'schema': 'invalid_schema',
        'entity_from_other_chunk': 'invalid_schema', 'timeout': 'privacy_timeout',
        'http': 'privacy_service_error'}[failure]


@pytest.mark.asyncio
@pytest.mark.parametrize('safe', [True, False])
async def test_review_requires_every_leaf_to_be_safe(monkeypatch, safe):
    calls = []
    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return capacity()
        return success(review=True, safe=safe if len(calls) == 3 else True,
            reasons=[] if safe else ['contact'])
    app = local_app(monkeypatch, respond)
    if safe:
        result = await local_check(app, None, None, '公开内容' * 1000, review=True)
        assert result['safe'] is True
    else:
        with pytest.raises(PrivacyUnavailable, match='联系方式') as caught:
            await local_check(app, None, None, '公开内容' * 1000, review=True)
        assert caught.value.code == 'residual_privacy'
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_total_timeout_is_not_reset_for_each_batch(monkeypatch):
    calls = []
    async def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return capacity()
        await asyncio.sleep(0.05)
        return success()
    with pytest.raises(PrivacyUnavailable) as caught:
        await local_check(local_app(monkeypatch, respond, timeout=0.08), None, None, '公开内容' * 1000)
    assert caught.value.code == 'privacy_timeout' and len(calls) == 3


@pytest.mark.asyncio
async def test_small_input_still_rejected_does_not_loop(monkeypatch):
    calls = []
    def respond(request):
        calls.append(request)
        return capacity(413)
    with pytest.raises(PrivacyUnavailable) as caught:
        await local_check(local_app(monkeypatch, respond), None, None, '短内容')
    assert len(calls) == 1 and caught.value.code == 'privacy_context_too_large'


@pytest.mark.asyncio
async def test_recursive_call_count_is_bounded_without_partial_success(monkeypatch):
    calls = []
    def respond(request):
        chunk = json.loads(request.content)['messages'][1]['content']
        calls.append(chunk)
        return capacity() if len(chunk) > 2000 else success()
    with pytest.raises(PrivacyUnavailable) as caught:
        await local_check(local_app(monkeypatch, respond), None, None, '甲' * 32768)
    assert caught.value.code == 'privacy_context_too_large'
    assert len(calls) == privacy_batches.MAX_CALLS


@pytest.mark.asyncio
async def test_recursion_depth_is_bounded(monkeypatch):
    calls = []
    def respond(request):
        calls.append(request)
        return capacity()
    with pytest.raises(PrivacyUnavailable) as caught:
        await local_check(local_app(monkeypatch, respond), None, None, '甲' * 32000)
    assert caught.value.code == 'privacy_context_too_large'
    assert len(calls) == privacy_batches.MAX_DEPTH + 1


def test_full_input_secret_guard_remains_before_batching():
    message = '公开内容' * 2000 + '密码：this-is-a-test-secret' + '公开内容' * 2000
    with pytest.raises(HTTPException, match='秘密'):
        _text_checked({'messages': [{'role': 'user', 'content': message}]})


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['detect_fail', 'review_fail', 'sensitive', 'success', 'secret'])
async def test_gateway_batches_preserve_outbound_and_consent_boundaries(system, alice, monkeypatch, mode):
    from sqlalchemy import select
    from homeai.cloud_gateway import CloudConsentRequired, invoke
    from homeai.contracts import TaskRequest
    from homeai.db import scope
    from homeai.model_routing import ModelUsage
    from homeai.runtime import submit
    from homeai.security import Actor
    from test_cloud_gateway import setup_cloud

    cloud = setup_cloud(system, alice)
    app = system[0].state
    local_traffic, cloud_traffic = [], []
    def respond(request):
        body = json.loads(request.content)
        if request.url.host == 'model.example':
            cloud_traffic.append(body)
            assert mode == 'success', '任一检测失败或缺少披露授权都不得访问云端'
            assert '张三' not in json.dumps(body, ensure_ascii=False)
            return httpx.Response(200, json={'choices': [{'message': {'content': '已完成'}}],
                                            'usage': {'total_tokens': 20}})
        chunk = body['messages'][1]['content']
        review = body['response_format']['json_schema']['name'] == 'privacy_review'
        local_traffic.append((review, chunk))
        if len(chunk) > 3000:
            return capacity()
        if mode == 'detect_fail' and not review and '尾段' in chunk:
            return success(uncertain=True)
        if review:
            return success(review=True, safe=not (mode == 'review_fail' and '尾段' in chunk))
        sensitive = mode == 'sensitive' and '尾段' in chunk
        return success(['张三'] if '张三' in chunk else [], sensitive=sensitive,
                       reasons=['health'] if sensitive else [])

    app.registry = local_app(monkeypatch, respond).registry
    app.registry.vault = app.vault
    actor = Actor(alice.user_id, 'h1', alice.device_id, 'adult')
    original = '公开摘要' * 1000 + '张三，尾段'
    if mode == 'secret':
        original += '密码：test-secret-for-guard'
    with system[2]() as db:
        scope(db, actor.user_id, actor.household_id)
        task = submit(db, actor, TaskRequest(message='隔离容量测试',
            idempotency_key='capacity-gateway-' + mode), app.vault)
        body = app.vault.open(task.request, actor.user_id + ':task:' + task.id)
        db.commit()
        arguments = {'messages': [{'role': 'user', 'content': original}], 'max_tokens': 100}
        if mode == 'success':
            result = await invoke(app, db, actor, cloud, task, body, arguments, 'batch-success')
            assert result['choices'][0]['message']['content'] == '已完成'
            assert len(cloud_traffic) == 1 and len(local_traffic) == 6
            assert db.scalar(select(ModelUsage)) is not None
        else:
            expected = CloudConsentRequired if mode == 'sensitive' else HTTPException if mode == 'secret' else PrivacyUnavailable
            with pytest.raises(expected):
                await invoke(app, db, actor, cloud, task, body, arguments, 'batch-stop')
            assert not cloud_traffic and db.scalar(select(ModelUsage)) is None
            assert body.get('_cloud_calls', 0) == 0
            if mode == 'secret':
                assert not local_traffic
