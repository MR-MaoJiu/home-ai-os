"""工具目录必须对应当前身份配置；仅使用隔离数据库与模型HTTP替身。"""
import json
import uuid

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from conftest import SignedClient
from homeai.contracts import ProviderManifest
from homeai.db import Invocation, Provider, Secret, Task, scope
from homeai.planner import decode_proposal, decode_proposals
from homeai.providers import Registry
from homeai.runtime import run_task
from homeai.security import Actor
from homeai.tool_availability import available_tools, configured_capability
from test_planning_resumption import setup_models


def actor(user):
    return Actor(user.user_id, 'h1', user.device_id, 'adult')


def catalog(system, user):
    with system[2]() as db:
        identity = actor(user)
        scope(db, identity.user_id, identity.household_id)
        tools, unavailable = available_tools(system[0].state, db, identity)
        return {tool['function']['name'] for tool in tools}, unavailable


def install(system, user, manifest, *, enabled=True, health='online', secret_owner=None, secret_provider=None, save_secret=True):
    with system[2]() as db:
        db.add(Provider(id=manifest.id, manifest=manifest.model_dump_json(), enabled=enabled, health=health))
        if manifest.secret_id and save_secret:
            owner = secret_owner or user
            db.add(Secret(id=manifest.secret_id, household_id='h1', owner_id=owner.user_id,
                provider_id=secret_provider or manifest.id,
                value=system[0].state.vault.seal('isolated-provider-key', owner.user_id + ':secret:' + manifest.secret_id)))
        db.commit()
    return manifest


def home(system, user, *, identifier='fixture.home', entities=None, secret=True, **options):
    manifest = ProviderManifest(id=identifier, version='1', adapter='homeassistant', endpoint='http://127.0.0.1:58123',
        allowed_hosts=['127.0.0.1'], capabilities={'home.states@v1': 'states'},
        secret_id=identifier + '.key' if secret else None,
        home_entities=['light.reading'] if entities is None else entities)
    return install(system, user, manifest, **options)


def configure_roles(user, **roles):
    response = user.request('GET', '/api/v1/manage/models')
    assert response.status_code == 200, response.text
    response = user.request('PUT', '/api/v1/manage/models', {**response.json()['configuration'], **roles})
    assert response.status_code == 200, response.text


def tool_reply(name, arguments=None):
    return {'id': str(uuid.uuid4()), 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments or {})}}


def test_absent_optional_providers_are_not_offered_but_core_tools_remain(system, alice):
    names, unavailable = catalog(system, alice)
    assert not {'read_home_states', 'search_web', 'generate_image', 'edit_image'} & names
    assert {'read_home_states', 'search_web', 'generate_image', 'edit_image'} <= unavailable.keys()
    assert {'create_reminder', 'schedule_reminder', 'search_documents', 'search_memories', 'search_calendar',
            'list_automations', 'create_automation', 'stop_automation', 'remember_chat', 'render_table',
            'send_resource', 'fetch_resource', 'collect_resources'} <= names
    assert all(isinstance(reason, str) and reason for reason in unavailable.values())


@pytest.mark.parametrize('missing', ['disabled', 'no_secret_reference', 'missing_secret', 'foreign_owner', 'wrong_provider', 'no_entities'])
def test_home_requires_enabled_provider_current_member_credential_and_entities(system, alice, missing):
    bob = SignedClient(system[1], system[2])
    home(system, alice, enabled=missing != 'disabled', secret=missing != 'no_secret_reference',
         save_secret=missing != 'missing_secret', secret_owner=bob if missing == 'foreign_owner' else alice,
         secret_provider='unrelated.provider' if missing == 'wrong_provider' else None,
         entities=[] if missing == 'no_entities' else ['light.reading'])
    names, unavailable = catalog(system, alice)
    assert 'read_home_states' not in names and 'read_home_states' in unavailable
    assert 'isolated-provider-key' not in json.dumps(unavailable)


@pytest.mark.parametrize('health', ['online', 'unavailable', 'offline', 'degraded'])
def test_home_is_offered_when_configured_even_during_temporary_health_failures(system, alice, health):
    home(system, alice, health=health)
    system[0].state.registry = Registry(system[0].state.vault,
        httpx.MockTransport(lambda request: pytest.fail('构造工具目录不应探测家居服务')))
    names, unavailable = catalog(system, alice)
    assert 'read_home_states' in names and 'read_home_states' not in unavailable
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        assert configured_capability(system[0].state, db, actor(alice), 'home.states@v1')


def test_home_directory_does_not_reuse_another_members_credentials(system, alice):
    bob = SignedClient(system[1], system[2])
    home(system, alice)
    assert 'read_home_states' in catalog(system, alice)[0]
    assert 'read_home_states' not in catalog(system, bob)[0]
    assert 'read_home_states' in catalog(system, alice)[0]


def test_home_selection_matches_the_provider_runtime_would_really_choose(system, alice):
    broken = home(system, alice, identifier='aa.empty-home', entities=[])
    home(system, alice, identifier='zz.configured-home')
    assert 'read_home_states' not in catalog(system, alice)[0]
    with system[2]() as db:
        db.get(Provider, broken.id).enabled = False
        db.commit()
    assert 'read_home_states' in catalog(system, alice)[0]


def test_web_search_follows_enabled_provider_not_health_or_other_tools(system, alice):
    search = install(system, alice, ProviderManifest(id='fixture.search', version='1', adapter='searxng',
        endpoint='http://127.0.0.1:58088', allowed_hosts=['127.0.0.1'], capabilities={'web.search@v1': '/search'}), enabled=False)
    assert 'search_web' not in catalog(system, alice)[0]
    with system[2]() as db:
        row = db.get(Provider, search.id)
        row.enabled, row.health = True, 'unavailable'
        db.commit()
    names, unavailable = catalog(system, alice)
    assert 'search_web' in names and 'search_web' not in unavailable
    assert 'read_home_states' not in names and 'create_reminder' in names


def test_image_tools_require_their_explicit_model_roles_not_vision_or_registration(system, alice):
    image = install(system, alice, ProviderManifest(id='fixture.image', version='1', adapter='openai',
        endpoint='http://127.0.0.1:58911/v1', allowed_hosts=['127.0.0.1'], model='image-model',
        capabilities={'image.generate@v1': 'generations', 'image.edit@v1': 'edits'}))
    vision = install(system, alice, ProviderManifest(id='fixture.vision', version='1', adapter='openai',
        endpoint='http://127.0.0.1:58912/v1', allowed_hosts=['127.0.0.1'], model='vision-model',
        capabilities={'model.generate@v1': 'chat'}))
    assert not {'generate_image', 'edit_image'} & catalog(system, alice)[0]
    configure_roles(alice, vision=vision.id)
    assert not {'generate_image', 'edit_image'} & catalog(system, alice)[0]
    configure_roles(alice, image_generation=image.id)
    names, _ = catalog(system, alice)
    assert 'generate_image' in names and 'edit_image' not in names
    configure_roles(alice, image_editing=image.id)
    assert {'generate_image', 'edit_image'} <= catalog(system, alice)[0]
    with system[2]() as db:
        db.get(Provider, image.id).enabled = False
        db.commit()
    assert not {'generate_image', 'edit_image'} & catalog(system, alice)[0]


@pytest.mark.parametrize('credentials', ['owned', 'missing', 'foreign_owner', 'wrong_provider'])
def test_image_role_with_declared_secret_requires_owned_or_shared_credential(system, alice, credentials):
    bob = SignedClient(system[1], system[2])
    image = install(system, alice, ProviderManifest(id='fixture.private-image', version='1', adapter='openai',
        endpoint='https://images.example.invalid/v1', cloud=True, allowed_hosts=['images.example.invalid'], model='image-model',
        secret_id='fixture.image-key', capabilities={'image.generate@v1': 'generations'}),
        save_secret=credentials != 'missing', secret_owner=bob if credentials == 'foreign_owner' else alice,
        secret_provider='unrelated.provider' if credentials == 'wrong_provider' else None)
    configure_roles(alice, image_generation=image.id)
    assert ('generate_image' in catalog(system, alice)[0]) == (credentials == 'owned')


def test_explicitly_shared_image_service_is_visible_and_revocation_removes_it(system, alice):
    setup_models(system, alice)
    bob = SignedClient(system[1], system[2])
    image = install(system, alice, ProviderManifest(id='fixture.shared-image', version='1', adapter='openai',
        endpoint='https://images.example.invalid/v1', cloud=True, allowed_hosts=['images.example.invalid'], model='image-model',
        secret_id='fixture.shared-image-key', capabilities={'image.generate@v1': 'generations'}))
    configure_roles(alice, image_generation=image.id)
    assert 'generate_image' in catalog(system, bob)[0]
    configure_roles(alice, shared_cloud=False)
    assert 'generate_image' not in catalog(system, bob)[0]
    assert 'generate_image' in catalog(system, alice)[0]


def test_decoder_rejects_tools_missing_from_the_actual_round_whitelist_before_returning_batch():
    home_response = {'choices': [{'message': {'tool_calls': [tool_reply('read_home_states')]}}]}
    for allowed in [set(), {'create_reminder'}]:
        with pytest.raises(HTTPException):
            decode_proposal(home_response, allowed_names=allowed)
    assert decode_proposal(home_response, allowed_names={'read_home_states'}) == ('home.states@v1', {})
    mixed = {'choices': [{'message': {'tool_calls': [tool_reply('create_reminder', {'title': '不应执行的提醒'}), tool_reply('read_home_states')]}}]}
    with pytest.raises(HTTPException):
        decode_proposals(mixed, max_calls=2, allowed_names={'create_reminder'})


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', ['unconfigured_home', 'configured_but_suppressed_round'])
async def test_agent_sends_filtered_tools_and_never_dispatches_an_unoffered_model_tool(system, alice, scenario):
    setup_models(system, alice)
    if scenario == 'configured_but_suppressed_round':
        home(system, alice)
    model_requests = []
    def transport(request):
        body = json.loads(request.content)
        if request.url.host == 'privacy.test':
            review = '"safe"' in body['messages'][0]['content']
            value = {'safe': True, 'uncertain': False} if review else {'entities': [], 'sensitive': False, 'uncertain': False}
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(value)}}]})
        assert request.url.host == 'model.example', '未提供的家居工具不应被执行'
        model_requests.append(body)
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': '',
            'tool_calls': [tool_reply('read_home_states')]}}], 'usage': {'total_tokens': 15}})
    app = system[0].state
    app.registry = Registry(app.vault, httpx.MockTransport(transport))
    created = alice.request('POST', '/api/v1/tasks', {'mode': 'cloud', 'message': '请根据现有资料说明当前情况。',
        'idempotency_key': str(uuid.uuid4()), 'max_model_tokens': 262144})
    assert created.status_code == 202, created.text
    task_id = created.json()['id']
    if scenario == 'configured_but_suppressed_round':
        with system[2]() as db:
            scope(db, alice.user_id, 'h1')
            task = db.get(Task, task_id)
            payload = app.vault.open(task.request, alice.user_id + ':task:' + task_id)
            payload['_answer_from_existing'] = True
            task.request = app.vault.seal(payload, alice.user_id + ':task:' + task_id)
            db.commit()
    await run_task(app, task_id, alice.user_id)
    assert len(model_requests) == 1
    names = {tool['function']['name'] for tool in model_requests[0].get('tools', [])}
    assert 'read_home_states' not in names
    if scenario == 'unconfigured_home':
        assert 'ask_user' in names
        assert 'create_reminder' not in names
    else:
        assert not names
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, task_id)
        assert task.status == 'FAILED'
        payload = app.vault.open(task.request, alice.user_id + ':task:' + task_id)
        assert payload['steps'] == []
        assert db.scalar(select(func.count()).select_from(Invocation).where(Invocation.task_id == task_id)) == 0
