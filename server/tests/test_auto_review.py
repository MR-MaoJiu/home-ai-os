"""临时数据库中走签名 API 和真实审批状态机，不向真实成员发送通知或外部副作用。"""
import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from conftest import SignedClient
from homeai.auto_review import process_pending
from homeai.crypto import canonical, digest
from homeai.db import Approval, Audit, Device, Invocation, Task, now, scope
from homeai.policy import CAPABILITIES, Policy
from homeai.runtime import run_task
from homeai.security import credential
from test_client_actions import new_task, request_action
from test_client_actions_health import health_parameters, snapshot, upload
from test_security_data import put, record

PATH = '/api/v1/devices/current/auto-review'


def toggle(client, enabled=True):
    result = client.request('PUT', PATH, {'enabled': enabled, 'acknowledged': enabled})
    assert result.status_code == 200, result.text
    return result.json()


async def process(system, client):
    return await process_pending(system[0].state, client.user_id, 'h1', 'infrastructure_owner')


def pending_approval(system, client, *, capability='mail.send@v1'):
    tid = new_task(system, client)
    with system[2]() as db:
        scope(db, client.user_id, 'h1')
        task = db.get(Task, tid)
        task.status = 'AWAITING_APPROVAL'
        arguments = {'to': 'test@example.invalid', 'subject': '隔离测试', 'text': '不会投递'}
        invocation = Invocation(id=str(uuid.uuid4()), owner_id=client.user_id, household_id='h1', task_id=tid,
            step=0, capability=capability, arguments_hash=digest(canonical(arguments)), arguments='')
        invocation.arguments = system[0].state.vault.seal(arguments, client.user_id + ':invocation:' + invocation.id)
        db.add(invocation)
        approval = Approval(id=str(uuid.uuid4()), owner_id=client.user_id, household_id='h1',
            invocation_id=invocation.id, arguments_hash=invocation.arguments_hash, expires_at=now() + 300)
        db.add(approval)
        db.commit()
        return tid, approval.id


def status(client, tid):
    return client.request('GET', '/api/v1/tasks/' + tid).json()['status']


def second_device(system, client):
    other = SignedClient(system[1], system[2])
    with system[2]() as db:
        db.get(Device, other.device_id).user_id = client.user_id
        other.token = credential(db, client.user_id, 'access', 3600, other.device_id)
        db.commit()
    other.user_id = client.user_id
    return other


def test_default_off_confirmation_and_signature(system, alice):
    assert alice.request('GET', PATH).json() == {
        'enabled': False, 'device_id': alice.device_id, 'scope': 'current_device_tasks'}
    assert system[1].put(PATH, json={'enabled': True, 'acknowledged': True}).status_code == 401
    assert alice.request('PUT', PATH, {'enabled': True}).status_code == 422
    assert alice.request('PUT', PATH, {'enabled': 'true', 'acknowledged': True}).status_code == 422
    assert alice.request('PUT', PATH, {'enabled': True, 'acknowledged': 'yes'}).status_code == 422
    assert toggle(alice)['enabled'] is True
    assert toggle(alice, False)['enabled'] is False
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        assert [row.action for row in db.scalars(select(Audit).where(Audit.action.like('auto_review.%')).order_by(Audit.created_at))] == ['auto_review.enabled', 'auto_review.disabled']


@pytest.mark.asyncio
async def test_explicit_approval_once_and_disabled_does_not_approve(system, alice):
    tid, aid = pending_approval(system, alice)
    assert await process(system, alice) == 0
    assert status(alice, tid) == 'AWAITING_APPROVAL'
    toggle(alice)
    assert await process(system, alice) == 1
    assert await process(system, alice) == 0
    assert status(alice, tid) == 'APPROVED'
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        assert db.get(Approval, aid).decision == 'APPROVED'
        entries = list(db.scalars(select(Audit).where(Audit.action == 'approval.approved')))
        assert len(entries) == 1 and json.loads(entries[0].details)['automatic'] is True
    second, _ = pending_approval(system, alice)
    toggle(alice, False)
    assert await process(system, alice) == 0
    assert status(alice, second) == 'AWAITING_APPROVAL'


@pytest.mark.asyncio
async def test_other_device_member_and_revoked_device_are_excluded(system, alice):
    alternate = second_device(system, alice)
    bob = SignedClient(system[1], system[2])
    first, _ = pending_approval(system, alice)
    other_device, _ = pending_approval(system, alternate)
    other_member, _ = pending_approval(system, bob)
    toggle(alice)
    assert await process(system, alice) == 1
    assert status(alice, first) == 'APPROVED'
    assert status(alternate, other_device) == 'AWAITING_APPROVAL'
    assert status(bob, other_member) == 'AWAITING_APPROVAL'
    revoked, aid = pending_approval(system, alice)
    assert alice.request('DELETE', '/api/v1/devices/' + alice.device_id).status_code == 200
    assert await process(system, alice) == 0
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        assert db.get(Approval, aid).decision == 'PENDING'
    assert alice.request('GET', PATH).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['expired', 'changed_arguments', 'canceled', 'deadline', 'r4', 'policy'])
async def test_automatic_approval_preserves_hard_guards(system, alice, monkeypatch, invalid):
    tid, aid = pending_approval(system, alice)
    toggle(alice)
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task, approval = db.get(Task, tid), db.get(Approval, aid)
        if invalid == 'expired': approval.expires_at = now() - 1
        if invalid == 'changed_arguments': approval.arguments_hash = 'changed'
        if invalid == 'canceled': task.cancel_requested = True
        if invalid == 'deadline': task.deadline = now() - 1
        db.commit()
    if invalid == 'r4': monkeypatch.setitem(CAPABILITIES, 'mail.send@v1', (4, True))
    if invalid == 'policy': system[0].state.policy = Policy('http://opa', httpx.MockTransport(lambda req: httpx.Response(200, json={'result': False})))
    assert await process(system, alice) == 0
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        assert db.get(Approval, aid).decision == 'PENDING'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['cloud.disclose', 'authorize_records', 'data.share'])
async def test_exact_own_record_or_disclosure_consent_resumes_once(system, alice, kind):
    rid = put(alice, record())
    tid = new_task(system, alice)
    parameters = {'record_ids': [rid]} if kind != 'cloud.disclose' else {
        'scope_hash': 'a' * 64, 'provider_id': 'cloud-test', 'record_versions': {rid: 1}}
    aid = request_action(system, alice, tid, kind, parameters)
    toggle(alice)
    assert await process(system, alice) == 1
    assert await process(system, alice) == 0
    assert status(alice, tid) == 'RECEIVED'
    assert alice.request('GET', '/api/v1/client-actions/' + aid).json()['status'] == 'RESPONDED'
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        entries = list(db.scalars(select(Audit).where(Audit.action == 'client.respond')))
        assert len(entries) == 1 and json.loads(entries[0].details)['automatic'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,parameters', [('provide_text', {}), ('choose_files', {}), ('choose_photos', {}),
    ('data.share', {}), ('capture_location', {}), ('choose_option', {'prompt': '选择测试项', 'options': [{'id': 'a', 'label': '测试'}]})])
async def test_never_invents_input_or_picks_unselected_data(system, alice, kind, parameters):
    tid = new_task(system, alice)
    aid = request_action(system, alice, tid, kind, parameters)
    toggle(alice)
    assert await process(system, alice) == 0
    assert alice.request('GET', '/api/v1/client-actions/' + aid).json()['status'] == 'PENDING'


@pytest.mark.asyncio
async def test_other_member_data_requires_that_member_despite_requester_auto_review(system, alice):
    bob = SignedClient(system[1], system[2])
    tid = new_task(system, alice, [bob])
    rid = put(bob, record())
    aid = request_action(system, alice, tid, 'data.share', {'record_ids': [rid]}, bob.user_id)
    toggle(alice)
    toggle(bob)
    assert await process(system, alice) == 0
    assert await process(system, bob) == 0
    assert bob.request('GET', '/api/v1/client-actions/' + aid).json()['status'] == 'PENDING'


@pytest.mark.asyncio
async def test_revoked_or_secret_record_is_not_auto_disclosed(system, alice):
    rid = put(alice, record())
    tid = new_task(system, alice)
    aid = request_action(system, alice, tid, 'cloud.disclose', {
        'scope_hash': 'a' * 64, 'provider_id': 'cloud-test', 'record_versions': {rid: 1}})
    toggle(alice)
    item = record(version=2)
    item['sensitivity'] = 'SECRET'
    put(alice, item)
    assert await process(system, alice) == 0
    assert alice.request('GET', '/api/v1/client-actions/' + aid).json()['status'] == 'PENDING'


def test_device_capture_flag_and_response_recheck(system, alice):
    parameters = health_parameters(['steps'])
    tid = new_task(system, alice)
    aid = request_action(system, alice, tid, 'read_health', parameters)
    path = '/api/v1/client-actions/' + aid
    assert alice.request('GET', path).json()['automatic_capture_allowed'] is False
    toggle(alice)
    assert alice.request('GET', path).json()['automatic_capture_allowed'] is True
    alternate = second_device(system, alice)
    toggle(alternate)
    assert alternate.request('GET', path).json()['automatic_capture_allowed'] is False
    rid = upload(alice, snapshot(parameters, ['steps']))
    body = {'idempotency_key': str(uuid.uuid4()), 'record_ids': [rid], 'automatic_review': True}
    assert alternate.request('POST', path + '/respond', body).status_code == 403
    toggle(alice, False)
    assert alice.request('POST', path + '/respond', body).status_code == 409
    assert alice.request('GET', path).json()['status'] == 'PENDING'
    toggle(alice)
    result = alice.request('POST', path + '/respond', body)
    assert result.status_code == 200, result.text
    assert result.json()['status'] == 'RESPONDED'
    assert alice.request('POST', path + '/respond', body).json() == result.json()
    assert alice.request('GET', path).json()['automatic_capture_allowed'] is False


@pytest.mark.asyncio
async def test_force_privacy_is_scoped_and_cannot_override_hard_failure(system, alice):
    from test_privacy_review import configure, paused, review
    configure(system, alice)
    tid, item = await paused(system, alice)
    toggle(alice)
    assert await process(system, alice) == 1
    value = review(alice, tid)
    assert value['status'] == 'approved'
    assert status(alice, tid) == 'RECEIVED'
    second, _ = await paused(system, alice)
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, second)
        body = system[0].state.vault.open(task.request, alice.user_id + ':task:' + second)
        body['_privacy_review']['overrideable'] = False
        task.request = system[0].state.vault.seal(body, alice.user_id + ':task:' + second)
        db.commit()
    assert await process(system, alice) == 0
    assert status(alice, second) == 'WAITING_PRIVACY'


@pytest.mark.asyncio
async def test_unanswerable_requests_do_not_starve_later_approval(system, alice):
    for _ in range(100):
        tid = new_task(system, alice)
        request_action(system, alice, tid, 'provide_text', {})
    tid, aid = pending_approval(system, alice)
    toggle(alice)
    assert await process(system, alice) == 0
    assert await process(system, alice) == 1
    assert status(alice, tid) == 'APPROVED'


def test_browser_identity_cannot_enable_device_auto_review(system, alice):
    from homeai.security import Actor, authenticate
    system[0].dependency_overrides[authenticate] = lambda: Actor(alice.user_id, 'h1', 'browser-session', 'infrastructure_owner')
    try:
        assert system[1].get(PATH).status_code == 403
        assert system[1].put(PATH, json={'enabled': True, 'acknowledged': True}).status_code == 403
    finally:
        system[0].dependency_overrides.pop(authenticate, None)


def test_legacy_manual_receipt_replays_with_explicit_false(system, alice):
    from homeai.client_actions import ClientAction
    tid = new_task(system, alice)
    aid = request_action(system, alice, tid, 'provide_text', {})
    path = '/api/v1/client-actions/' + aid + '/respond'
    body = {'idempotency_key': str(uuid.uuid4()), 'record_ids': [], 'text': '隔离兼容测试'}
    original = alice.request('POST', path, body)
    assert original.status_code == 200, original.text
    # 旧版没有 automatic_review 字段，数据库回执摘要必须保持原算法。
    legacy_hash = digest(canonical({'decision': 'respond', **body}))
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        assert db.get(ClientAction, aid).response_hash == legacy_hash
    replay = alice.request('POST', path, {**body, 'automatic_review': False})
    assert replay.status_code == 200 and replay.json() == original.json()


def test_automatic_receipt_cannot_replay_as_manual(system, alice):
    tid = new_task(system, alice)
    aid = request_action(system, alice, tid, 'cloud.disclose', {
        'scope_hash': 'a' * 64, 'provider_id': 'cloud-test', 'record_versions': {}})
    toggle(alice)
    path = '/api/v1/client-actions/' + aid + '/respond'
    body = {'idempotency_key': str(uuid.uuid4()), 'automatic_review': True}
    original = alice.request('POST', path, body)
    assert original.status_code == 200, original.text
    assert alice.request('POST', path, body).json() == original.json()
    assert alice.request('POST', path, {**body, 'automatic_review': False}).status_code == 409
    assert alice.request('POST', path, {'idempotency_key': body['idempotency_key']}).status_code == 409
