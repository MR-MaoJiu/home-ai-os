"""历史上下文与云披露的任务边界；仅 HTTP 模型响应使用隔离替身。"""
import json
import uuid

import httpx
import pytest

from homeai.client_actions import create_request
from homeai.cloud_gateway import CloudConsentRequired, invoke
from homeai.contracts import ProviderManifest
from homeai.conversations import history
from homeai.db import Provider, Task, scope
from homeai.model_routing import ModelVerification, fingerprint
from homeai.providers import Registry
from homeai.security import Actor
from test_cloud_gateway import setup_cloud
from test_conversations import finish, new_chat
from test_media import upload
from test_security_data import put, record


PRIVATE_MARKER = '历史私密原文标记'


def test_failed_tool_list_and_null_review_do_not_break_new_history(system, alice):
    conversation_id = new_chat(alice)
    old = send(alice, conversation_id, '检索公开说明')
    app = system[0].state
    with system[2]() as db:
        task, body = read_task(system, alice, db, old)
        body['_privacy_review'] = None
        task.request = app.vault.seal(body, alice.user_id + ':task:' + task.id)
        task.result = app.vault.seal([{'content': PRIVATE_MARKER}], alice.user_id + ':task-result:' + task.id)
        task.status, task.error = 'FAILED', '工具查询后的模型处理未完成'
        db.commit()
    current = send(alice, conversation_id, '解释彩虹形成的原理')
    with system[2]() as db:
        _, body = read_task(system, alice, db, current)
        messages = history(app, db, actor_for(alice), body)
        assert PRIVATE_MARKER not in json.dumps(messages, ensure_ascii=False)


@pytest.fixture
def scoped_cloud(system, alice):
    """保留真实本地检测、披露许可、来源校验与用量路径，不调用公网。"""
    app = system[0].state
    cloud = setup_cloud(system, alice)
    local = ProviderManifest(id='privacy.history', version='1', adapter='openai',
        endpoint='http://local.test/v1', model='privacy', allowed_hosts=['local.test'],
        capabilities={'model.generate@v1': 'chat'})
    with system[2]() as db:
        db.add(Provider(id=local.id, manifest=local.model_dump_json(), enabled=True))
        db.add(ModelVerification(id='h1:' + local.id, household_id='h1', provider_id=local.id,
            fingerprint=fingerprint(local), report=json.dumps({'privacy': True, 'text': True})))
        db.commit()
    traffic = []

    def transport(request):
        value = json.loads(request.content)
        traffic.append((request.url.host, value))
        if request.url.host == 'local.test':
            is_review = '"safe"' in value['messages'][0]['content']
            checked = value['messages'][-1]['content']
            verdict = {'safe': True, 'uncertain': False} if is_review else {
                'entities': [], 'sensitive': PRIVATE_MARKER in checked, 'uncertain': False,
                'reason_codes': ['health'] if PRIVATE_MARKER in checked else [],
            }
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(verdict)}}]})
        assert request.url.host == 'model.example'
        return httpx.Response(200, json={'choices': [{'message': {'content': '公开测试回答'}}],
            'usage': {'total_tokens': 12}})

    app.registry = Registry(app.vault, httpx.MockTransport(transport))
    return cloud, traffic


def actor_for(alice):
    return Actor(alice.user_id, 'h1', alice.device_id, 'infrastructure_owner')


def send(alice, conversation_id, content, **extra):
    response = alice.request('POST', '/api/v1/conversations/' + conversation_id + '/messages',
        {'client_key': str(uuid.uuid4()), 'content': content, **extra})
    assert response.status_code == 202, response.text
    return response.json()


def completed_private_turn(system, alice, conversation_id, kind='health.steps', *, label='健康', approval=False):
    item = record(source_id=str(uuid.uuid4()), kind=kind, payload={'title': label + '历史资料'})
    item['cloud_policy'] = 'LOCAL_ONLY'
    identifier = put(alice, item)
    turn = send(alice, conversation_id, '分析' + label + '资料：' + PRIVATE_MARKER,
        references=[{'record_id': identifier, 'version': 1}])
    action_id = None
    if approval:
        app = system[0].state
        with system[2]() as db:
            scope(db, alice.user_id, 'h1')
            task = db.get(Task, turn['task_id'])
            result = create_request(app, db, actor_for(alice), task, 'cloud.disclose', '本轮资料披露',
                {'scope_hash': 'a' * 64, 'record_versions': {identifier: 1}, 'provider_id': 'cloud.test'})
            action_id = result['client_action_id']
            db.commit()
        accepted = alice.request('POST', '/api/v1/client-actions/' + action_id + '/respond',
            {'idempotency_key': 'history-approval-' + str(uuid.uuid4())})
        assert accepted.status_code == 200, accepted.text
    finish(system, alice, turn['task_id'], label + '旧答案：' + PRIVATE_MARKER)
    return identifier, turn, action_id


def read_task(system, alice, db, turn):
    scope(db, alice.user_id, 'h1')
    task = db.get(Task, turn['task_id'])
    body = system[0].state.vault.open(task.request, alice.user_id + ':task:' + task.id)
    return task, body


async def history_to_cloud(system, alice, db, turn, manifest):
    task, body = read_task(system, alice, db, turn)
    messages = history(system[0].state, db, actor_for(alice), body)
    result = await invoke(system[0].state, db, actor_for(alice), manifest, task, body,
        {'messages': [*messages, {'role': 'user', 'content': body['message']}], 'max_tokens': 128},
        'history-scope-' + task.id)
    return body, messages, result


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['health.steps', 'location.current', 'calendar.event'])
async def test_unrelated_question_excludes_old_private_text_dependencies_and_consent(system, alice, scoped_cloud, kind):
    manifest, traffic = scoped_cloud
    conversation_id = new_chat(alice)
    identifier, old_turn, old_approval = completed_private_turn(system, alice, conversation_id, kind, approval=True)
    current = send(alice, conversation_id, '请解释为什么天空是蓝色的。')
    with system[2]() as db:
        body, messages, result = await history_to_cloud(system, alice, db, current, manifest)
        assert result['choices'][0]['message']['content'] == '公开测试回答'
        assert PRIVATE_MARKER not in json.dumps(messages, ensure_ascii=False)
        assert identifier not in body.get('_record_dependencies', {})
        assert identifier not in body.get('_historical_record_ids', [])
        assert old_approval not in body.get('_cloud_consents', [])
    cloud_requests = [value for host, value in traffic if host == 'model.example']
    assert len(cloud_requests) == 1
    assert PRIVATE_MARKER not in json.dumps(cloud_requests, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit_reply', [False, True])
async def test_followup_selects_target_only_and_requires_new_cloud_approval(system, alice, scoped_cloud, explicit_reply):
    manifest, traffic = scoped_cloud
    conversation_id = new_chat(alice)
    if explicit_reply:
        selected, original, old_approval = completed_private_turn(system, alice, conversation_id, approval=True)
        unrelated, _, _ = completed_private_turn(system, alice, conversation_id, 'location.current', label='位置')
    else:
        unrelated, _, _ = completed_private_turn(system, alice, conversation_id, 'location.current', label='位置')
        selected, original, old_approval = completed_private_turn(system, alice, conversation_id, approval=True)
    arguments = {'reply_to_task_id': original['task_id']} if explicit_reply else {}
    followup = send(alice, conversation_id, '请继续分析刚才那份健康资料。', **arguments)
    with system[2]() as db:
        task, body = read_task(system, alice, db, followup)
        messages = history(system[0].state, db, actor_for(alice), body)
        assert body.get('_record_dependencies', {}) == {selected: 1}
        assert unrelated not in body.get('_historical_record_ids', [])
        assert old_approval not in body.get('_cloud_consents', [])
        with pytest.raises(CloudConsentRequired) as required:
            await invoke(system[0].state, db, actor_for(alice), manifest, task, body,
                {'messages': [*messages, {'role': 'user', 'content': body['message']}]}, 'new-followup')
        assert required.value.record_versions == {selected: 1}
        assert required.value.scope_hash != 'a' * 64
    assert not any(host == 'model.example' for host, _ in traffic)


@pytest.mark.asyncio
async def test_new_attachment_does_not_select_previous_private_attachments(system, alice, scoped_cloud):
    manifest, _ = scoped_cloud
    conversation_id = new_chat(alice)
    private, _, _ = completed_private_turn(system, alice, conversation_id)
    uploaded = upload(alice, b'Public astronomy facts only.', 'astronomy.txt', 'file')
    assert uploaded.status_code == 200, uploaded.text
    current_id = uploaded.json()['asset']['record_id']
    current = send(alice, conversation_id, '总结这份文件。',
        parts=[{'type': 'file', 'record_id': current_id, 'version': 1}])
    with system[2]() as db:
        task, body = read_task(system, alice, db, current)
        messages = history(system[0].state, db, actor_for(alice), body)
        assert body.get('_record_dependencies', {}) == {current_id: 1}
        assert private not in body.get('_historical_record_ids', [])
        assert PRIVATE_MARKER not in json.dumps(messages, ensure_ascii=False)
        # 当前上传文件仍遵循自身原有的 LOCAL_ONLY 规则，只能为本文件申请。
        with pytest.raises(CloudConsentRequired) as required:
            await invoke(system[0].state, db, actor_for(alice), manifest, task, body,
                {'messages': [*messages, {'role': 'user', 'content': body['message']}]}, 'new-file')
        assert required.value.record_versions == {current_id: 1}


@pytest.mark.asyncio
async def test_plain_multiturn_context_is_retained_after_private_task(system, alice, scoped_cloud):
    manifest, traffic = scoped_cloud
    conversation_id = new_chat(alice)
    completed_private_turn(system, alice, conversation_id)
    ordinary = send(alice, conversation_id, '本次公开演示的代号是青竹。')
    finish(system, alice, ordinary['task_id'], '收到，公开演示代号是青竹。')
    current = send(alice, conversation_id, '刚才的公开演示代号是什么？')
    with system[2]() as db:
        body, messages, _ = await history_to_cloud(system, alice, db, current, manifest)
        assert '青竹' in json.dumps(messages, ensure_ascii=False)
        assert PRIVATE_MARKER not in json.dumps(messages, ensure_ascii=False)
        assert body.get('_record_dependencies', {}) == {}
    assert any(host == 'model.example' for host, _ in traffic)


@pytest.mark.asyncio
async def test_old_summary_and_transitive_dependencies_do_not_reintroduce_private_source(system, alice, scoped_cloud):
    manifest, _ = scoped_cloud
    conversation_id = new_chat(alice)
    private, _, _ = completed_private_turn(system, alice, conversation_id)
    contaminated = send(alice, conversation_id, '旧实现已经被污染的普通问答')
    with system[2]() as db:
        task, body = read_task(system, alice, db, contaminated)
        # 兼容已有任务：原实现把前序来源无条件并入普通任务的依赖。
        body['_record_dependencies'] = {private: 1}
        task.request = system[0].state.vault.seal(body, alice.user_id + ':task:' + task.id)
        db.commit()
    finish(system, alice, contaminated['task_id'], '普通回答也携带旧来源：' + PRIVATE_MARKER)
    # 足量正常历史确保旧实现会走历史摘录分支，而不只验证最近对话。
    for index in range(3):
        ordinary = send(alice, conversation_id, f'公开写作样例第{index}轮')
        finish(system, alice, ordinary['task_id'], '公开文本' * 600)
    current = send(alice, conversation_id, '用一句话解释彩虹形成的原因。')
    with system[2]() as db:
        body, messages, _ = await history_to_cloud(system, alice, db, current, manifest)
        assert private not in body.get('_record_dependencies', {})
        assert PRIVATE_MARKER not in json.dumps(messages, ensure_ascii=False)
        assert contaminated['id'] not in body.get('_history_summary_sources', [])


@pytest.mark.asyncio
async def test_text_only_approved_private_history_does_not_prompt_for_unrelated_question(system, alice, scoped_cloud):
    manifest, traffic = scoped_cloud
    conversation_id = new_chat(alice)
    original = send(alice, conversation_id, '我本次提供的健康情况：' + PRIVATE_MARKER)
    app = system[0].state
    with system[2]() as db:
        task, body = read_task(system, alice, db, original)
        with pytest.raises(CloudConsentRequired) as required:
            await invoke(app, db, actor_for(alice), manifest, task, body,
                {'messages': [{'role': 'user', 'content': body['message']}]}, 'text-only-original')
        needed = required.value
        pending = create_request(app, db, actor_for(alice), task, 'cloud.disclose', needed.detail,
            {'scope_hash': needed.scope_hash, 'record_versions': {}, 'provider_id': manifest.id})
        db.commit()
    response = alice.request('POST', '/api/v1/client-actions/' + pending['client_action_id'] + '/respond',
        {'idempotency_key': 'text-only-approval'})
    assert response.status_code == 200, response.text
    finish(system, alice, original['task_id'], '旧健康回答：' + PRIVATE_MARKER)
    traffic.clear()
    current = send(alice, conversation_id, '请解释普通二进制计数原理。')
    with system[2]() as db:
        _, messages, _ = await history_to_cloud(system, alice, db, current, manifest)
        assert PRIVATE_MARKER not in json.dumps(messages, ensure_ascii=False)
    assert len([host for host, _ in traffic if host == 'model.example']) == 1


@pytest.mark.asyncio
async def test_derived_document_provenance_is_not_carried_into_unrelated_turn(system, alice, scoped_cloud):
    from test_task_derived_sources import parsed_file, binding
    manifest, _ = scoped_cloud
    source, child = await parsed_file(system, alice)
    conversation_id = new_chat(alice)
    previous = send(alice, conversation_id, '总结私人文档',
        parts=[{'type': 'file', 'record_id': source, 'version': 1}])
    with system[2]() as db:
        task, body = read_task(system, alice, db, previous)
        body.update(binding(source, child))
        task.request = system[0].state.vault.seal(body, alice.user_id + ':task:' + task.id)
        db.commit()
    finish(system, alice, previous['task_id'], '文档正文：' + PRIVATE_MARKER)
    current = send(alice, conversation_id, '解释太阳为什么会发光。')
    with system[2]() as db:
        body, messages, _ = await history_to_cloud(system, alice, db, current, manifest)
        assert body.get('_record_dependencies', {}) == {}
        assert body.get('_derived_task_sources', {}) == {}
        assert PRIVATE_MARKER not in json.dumps(messages, ensure_ascii=False)


@pytest.mark.asyncio
async def test_followup_preserves_clean_user_fact_from_legacy_contaminated_turn(system, alice, scoped_cloud):
    manifest, traffic = scoped_cloud
    conversation_id = new_chat(alice)
    health, _, _ = completed_private_turn(system, alice, conversation_id)
    ordinary = send(alice, conversation_id, '公开演示的代号是青竹。')
    with system[2]() as db:
        task, body = read_task(system, alice, db, ordinary)
        body['_record_dependencies'] = {health: 1}
        task.request = system[0].state.vault.seal(body, alice.user_id + ':task:' + task.id)
        db.commit()
    finish(system, alice, ordinary['task_id'], '青竹的旧回答混入健康结果：' + PRIVATE_MARKER)
    current = send(alice, conversation_id, '刚才的公开演示代号是什么？')
    with system[2]() as db:
        body, messages, _ = await history_to_cloud(system, alice, db, current, manifest)
        encoded = json.dumps(messages, ensure_ascii=False)
        assert '青竹' in encoded
        assert PRIVATE_MARKER not in encoded
        assert health not in body.get('_record_dependencies', {})
        assert not body.get('_history_requires_disclosure')
    cloud = [value for host, value in traffic if host == 'model.example']
    assert len(cloud) == 1
    assert '青竹' in json.dumps(cloud, ensure_ascii=False)
    assert PRIVATE_MARKER not in json.dumps(cloud, ensure_ascii=False)


@pytest.mark.asyncio
async def test_repeated_history_keeps_new_health_dependency_acquired_by_current_task(system, alice, scoped_cloud):
    manifest, traffic = scoped_cloud
    conversation_id = new_chat(alice)
    old_health, _, _ = completed_private_turn(system, alice, conversation_id)
    current = send(alice, conversation_id, '请处理这次新提供的数据。')
    new_health = put(alice, record(source_id='current-tool-health', kind='health.steps',
        payload={'title': '本次读取的健康样本'}))
    with system[2]() as db:
        task, body = read_task(system, alice, db, current)
        first = history(system[0].state, db, actor_for(alice), body)
        assert old_health not in body.get('_record_dependencies', {})
        # 模拟本任务的真实工具已完成读取后的依赖登记，不改变旧任务来源。
        body.setdefault('_record_dependencies', {})[new_health] = 1
        second = history(system[0].state, db, actor_for(alice), body)
        assert body.get('_record_dependencies', {}) == {new_health: 1}
        assert PRIVATE_MARKER not in json.dumps([first, second], ensure_ascii=False)
        with pytest.raises(CloudConsentRequired) as required:
            await invoke(system[0].state, db, actor_for(alice), manifest, task, body,
                {'messages': [*second, {'role': 'user', 'content': body['message']}]}, 'current-health')
        assert required.value.record_versions == {new_health: 1}
    assert not any(host == 'model.example' for host, _ in traffic)


@pytest.mark.parametrize('initial_selection', ['history', 'current'])
def test_history_selection_stays_fixed_when_current_task_record_ids_change(system, alice, initial_selection):
    conversation_id = new_chat(alice)
    selected, previous, _ = completed_private_turn(system, alice, conversation_id)
    fresh = put(alice, record(source_id='selection-current-source', kind='calendar.event',
        payload={'title': '本次新来源'}))
    current = send(alice, conversation_id, '刚才的健康资料再分析一下。')
    with system[2]() as db:
        _, body = read_task(system, alice, db, current)
        if initial_selection == 'current':
            body['record_ids'] = [fresh]
            body['_record_dependencies'] = {fresh: 1}
        history(system[0].state, db, actor_for(alice), body)
        before = body['_history_followup_turn']
        if initial_selection == 'history':
            assert before == previous['id']
            assert selected in body.get('_historical_record_ids', [])
            body['record_ids'] = [fresh]
            body.setdefault('_record_dependencies', {})[fresh] = 1
        else:
            assert before is None
            assert selected not in body.get('_historical_record_ids', [])
            body['record_ids'] = []
        after_messages = history(system[0].state, db, actor_for(alice), body)
        assert body['_history_followup_turn'] == before
        if initial_selection == 'history':
            assert selected in body.get('_historical_record_ids', [])
            assert body['_record_dependencies'] == {selected: 1, fresh: 1}
        else:
            assert selected not in body.get('_record_dependencies', {})
            assert PRIVATE_MARKER not in json.dumps(after_messages, ensure_ascii=False)


@pytest.mark.asyncio
async def test_unrelated_question_does_not_inherit_other_members_shared_source(system, alice, scoped_cloud):
    from conftest import SignedClient
    manifest, traffic = scoped_cloud
    other = SignedClient(system[1], system[2])
    item = record(source_id='other-member-shared-note', kind='calendar.event',
        payload={'title': '其他成员共享的公开安排'})
    item.update(sensitivity='PRIVATE', cloud_policy='REDACT_AND_ALLOW')
    shared = put(other, item)
    assert other.request('PUT', '/api/v1/data/' + shared + '/visibility',
        {'visibility': 'family'}).status_code == 200
    conversation_id = new_chat(alice)
    previous = send(alice, conversation_id, '读取家庭共享安排。',
        references=[{'record_id': shared, 'version': 1}])
    # 本地读取家庭共享资料不等于其他成员批准了后续任务向云端发送该资料。
    finish(system, alice, previous['task_id'], '共享安排来源标记：其他成员原始资料。')
    current = send(alice, conversation_id, '请解释整数和分数有什么区别。')
    with system[2]() as db:
        body, messages, result = await history_to_cloud(system, alice, db, current, manifest)
        assert result['choices'][0]['message']['content'] == '公开测试回答'
        assert shared not in body.get('_record_dependencies', {})
        assert shared not in body.get('_historical_record_ids', [])
        assert '其他成员原始资料' not in json.dumps(messages, ensure_ascii=False)
        assert not body.get('_cloud_consents')
    cloud = [value for host, value in traffic if host == 'model.example']
    assert len(cloud) == 1
    assert '其他成员原始资料' not in json.dumps(cloud, ensure_ascii=False)
