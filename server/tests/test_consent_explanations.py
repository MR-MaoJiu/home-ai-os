"""文案仅描述本次命中项；隔离传输验证，不调用真实模型或设备。"""
import json
from types import SimpleNamespace

import httpx
import pytest

from homeai import cloud_gateway
from homeai.cloud_gateway import CloudConsentRequired, PrivacyUnavailable, invoke, local_check
from homeai.contracts import TaskRequest
from homeai.db import Record, scope
from homeai.privacy_review import display_failure
from homeai.providers import Registry
from homeai.runtime import submit
from homeai.security import Actor
from test_cloud_gateway import setup_cloud
from test_security_data import put, record


@pytest.mark.asyncio
@pytest.mark.parametrize(('parsed', 'stage', 'expected', 'absent'), [
    ({'uncertain': True, 'reason_codes': ['health'], 'reason': '私人健康原文'}, 'detect', '健康信息', ['姓名', '精确位置', '私人健康原文']),
    ({'uncertain': True, 'reason_codes': []}, 'detect', '未返回具体类别', ['健康', '姓名', '个人信息']),
    ({'uncertain': False, 'safe': False, 'reason_codes': ['contact']}, 'review', '联系方式', ['健康', '地址', '精确位置']),
    ({'uncertain': False, 'safe': False, 'reason_codes': ['unknown']}, 'review', '未返回具体类别', ['健康', '姓名', '个人信息']),
])
async def test_local_failure_reports_only_returned_category(monkeypatch, parsed, stage, expected, absent):
    async def allow(*args): pass
    async def respond(*args):
        return {'choices': [{'message': {'content': json.dumps(parsed)}}]}
    monkeypatch.setattr(cloud_gateway, 'resolve_role', lambda *args: SimpleNamespace(cloud=False, timeout_seconds=30))
    app = SimpleNamespace(policy=SimpleNamespace(check=allow), registry=SimpleNamespace(invoke=respond))
    with pytest.raises(PrivacyUnavailable) as failure:
        await local_check(app, None, None, '隔离样例', review=stage == 'review')
    error = failure.value
    assert expected in error.detail and error.stage == stage
    assert error.overrideable and error.retryable
    assert all(text not in error.detail for text in absent)


@pytest.mark.asyncio
async def test_detection_timeout_does_not_invent_content_problem(monkeypatch):
    async def allow(*args): pass
    async def timeout(*args): raise TimeoutError()
    monkeypatch.setattr(cloud_gateway, 'resolve_role', lambda *args: SimpleNamespace(cloud=False, timeout_seconds=30))
    app = SimpleNamespace(policy=SimpleNamespace(check=allow), registry=SimpleNamespace(invoke=timeout))
    with pytest.raises(PrivacyUnavailable) as failure:
        await local_check(app, None, None, '隔离样例')
    error = failure.value
    assert error.code == 'privacy_timeout' and error.overrideable
    assert '超时' in error.detail and error.suggestion == '请重新检查本次内容。'
    assert all(text not in error.detail + error.suggestion for text in ['健康', '位置', '修改', '敏感'])


@pytest.mark.asyncio
@pytest.mark.parametrize(('sensitive', 'policy', 'kind', 'detected', 'expected', 'absent'), [
    ('PRIVATE', 'LOCAL_ONLY', 'file.document', {}, ['仅本地处理'], ['健康', '精确位置', '图像', '敏感']),
    ('SENSITIVE', 'REDACT_AND_ALLOW', 'health.steps', {}, ['健康信息'], ['仅本地', '图像', '位置']),
    ('PRIVATE', 'REDACT_AND_ALLOW', 'file.document', {'sensitive': True, 'reason_codes': ['precise_location']}, ['精确位置'], ['健康', '图像', '仅本地']),
])
async def test_cloud_consent_lists_actual_trigger_and_keeps_request_scope(system, alice, monkeypatch, sensitive, policy, kind, detected, expected, absent):
    manifest = setup_cloud(system, alice)
    rid = put(alice, record())
    app = system[0].state
    app.registry = Registry(app.vault, httpx.MockTransport(lambda request: pytest.fail('授权前不得出站')))
    async def check(*args, **kwargs):
        return {'entities': [], 'sensitive': False, 'uncertain': False, **detected}
    monkeypatch.setattr(cloud_gateway, 'local_check', check)
    actor = Actor(alice.user_id, 'h1', alice.device_id, 'infrastructure_owner')
    with system[2]() as db:
        scope(db, actor.user_id, actor.household_id)
        source = db.get(Record, rid)
        source.sensitivity, source.cloud_policy, source.kind = sensitive, policy, kind
        task = submit(db, actor, TaskRequest(message='当前资料的隔离测试', record_ids=[rid], idempotency_key='consent-scope-test'), app.vault)
        body = app.vault.open(task.request, actor.user_id + ':task:' + task.id)
        db.commit()
        with pytest.raises(CloudConsentRequired) as failure:
            await invoke(app, db, actor, manifest, task, body, {'messages': [{'role': 'user', 'content': '隔离资料'}]}, 'consent-copy')
        error = failure.value
        assert all(text in error.detail for text in expected)
        assert all(text not in error.detail for text in absent)
        assert error.target_member_id == actor.user_id and error.record_versions == {rid: 1}
        assert len(error.scope_hash) == 64 and error.provider_id == manifest.id


def test_legacy_projection_changes_known_template_only():
    old = {'code': 'privacy_timeout', 'stage': 'detect', 'reason': '本地隐私检测超时，尚未向云端发送',
           'suggestion': '可修改待发送文本后重试，或确认仅对本次内容跳过本地模型的隐私判断。',
           'overrideable': True, 'retryable': True}
    projected = display_failure(old)
    assert projected['suggestion'] == '请重新检查本次内容。'
    assert {key: value for key, value in old.items() if key != 'suggestion'} == {key: value for key, value in projected.items() if key != 'suggestion'}
    assert '修改' in old['suggestion']
    specific = {**old, 'reason': '这个模型的具体故障', 'suggestion': '这个故障的具体修复步骤'}
    assert display_failure(specific) == specific


def test_legacy_action_projection_does_not_invent_categories():
    from homeai.client_actions import display_purpose
    old = '当前任务需要披露敏感或仅本地资料，请单独确认'
    assert display_purpose('cloud.disclose', old) == '请确认本次所选资料的云端披露。'
    assert display_purpose('capture_location', old) == old
    assert display_purpose('cloud.disclose', '本次将发送健康信息') == '本次将发送健康信息'


def test_task_error_projection_preserves_authoritative_error_and_other_states():
    from homeai.privacy_review import display_task_error
    task = SimpleNamespace(status='WAITING_PRIVACY', error='本地隐私模型已停用或尚未通过能力验证')
    body = {'_privacy_failure': {'reason': '另一轮旧错误'}}
    assert display_task_error(task, body) == '本地隐私模型当前不可用'
    assert task.error == '本地隐私模型已停用或尚未通过能力验证'
    task.status = 'FAILED'
    assert display_task_error(task, body) == task.error
    task.status, task.error = 'WAITING_PRIVACY', '本次具体故障'
    assert display_task_error(task, body) == '本次具体故障'


@pytest.mark.parametrize('mode', ['own_cloud', 'member_data', 'member_cloud'])
def test_legacy_pending_request_reuses_unchanged_payload_and_hash(system, alice, mode):
    from conftest import SignedClient
    from test_client_actions import new_task, actor
    from homeai.client_actions import ClientAction, create_request
    from homeai.crypto import canonical, digest
    from homeai.db import Task
    app = system[0].state
    bob = SignedClient(system[1], system[2])
    tid = new_task(system, alice, [bob])
    target = alice.user_id if mode == 'own_cloud' else bob.user_id
    kind = 'data.share' if mode == 'member_data' else 'cloud.disclose'
    params = {'record_ids': []} if kind == 'data.share' else {'scope_hash': 'a' * 64, 'record_versions': {}, 'provider_id': 'cloud.test'}
    if mode == 'member_cloud':
        rid = put(bob, record())
        assert bob.request('PUT', '/api/v1/data/' + rid + '/visibility', {'visibility': 'family'}).status_code == 200
        params['record_versions'] = {rid: 1}
    legacy = {'own_cloud': '当前任务需要披露敏感或仅本地资料，请单独确认',
              'member_data': '成员请求你为本次任务提供资料。仅你主动选择的内容会用于这次任务，不会改成家庭共享。',
              'member_cloud': '允许本次任务向指定云模型披露所选资料的脱敏内容'}[mode]
    purpose = '本次将向云模型发送健康信息，请确认。' if mode == 'own_cloud' else '当前所需资料'
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, tid)
        result = create_request(app, db, actor(alice), task, kind, purpose, params, target_member_id=target, request_key='legacy-copy')
        action = db.get(ClientAction, result['client_action_id'])
        action.payload = app.vault.seal({'purpose': legacy, 'parameters': params}, target + ':client-action:' + action.id)
        action.request_hash = digest(canonical({'kind': kind, 'purpose': legacy, 'parameters': params, 'target': target}))
        db.commit()
        original = (action.payload, action.request_hash, action.id, action.expires_at)
        replay = create_request(app, db, actor(alice), task, kind, purpose, params, target_member_id=target, request_key='legacy-copy')
        db.flush()
        assert replay['client_action_id'] == action.id
        assert (action.payload, action.request_hash, action.id, action.expires_at) == original
        if mode == 'own_cloud':
            from fastapi import HTTPException
            for changed_purpose, changed_params in [
                ('本次将向云模型发送任意新增内容，请确认。', params),
                (purpose, {**params, 'provider_id': 'different.model'}),
                (purpose, {**params, 'scope_hash': 'b' * 64}),
            ]:
                with pytest.raises(HTTPException) as blocked:
                    create_request(app, db, actor(alice), task, kind, changed_purpose, changed_params, target_member_id=target, request_key='legacy-copy')
                assert blocked.value.status_code == 409
            assert (action.payload, action.request_hash, action.id, action.expires_at) == original
