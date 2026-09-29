"""通知隔离、服务端调度与 APNs 协议测试；模拟传输不计作真机推送验收。"""
import base64
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from sqlalchemy import select

from conftest import SignedClient
from homeai.data import emit
from homeai.db import (Automation, Consumption, Device, Notification, PushDelivery,
                       PushRegistration, Record, Task, now, scope)
from homeai.notifications import APNs, collect_due_reminders, collect_task_events, deliver, push_configuration
from homeai.security import Actor
from test_security_data import put, record


def register(client, token='ab' * 32):
    return client.request('PUT', '/api/v1/devices/current/push',
                          {'token': token, 'environment': 'sandbox', 'authorization': 'authorized'})


def completed_task(system, client, automation=None, status='SUCCEEDED'):
    response = client.request('POST', '/api/v1/tasks', {'idempotency_key': str(uuid.uuid4()), 'message': '不会写入通知的私人正文'})
    assert response.status_code == 202
    identifier = response.json()['id']
    with system[2]() as db:
        scope(db, client.user_id, 'h1')
        row = db.get(Task, identifier)
        row.status = status
        if automation:
            body = system[0].state.vault.open(row.request, client.user_id + ':task:' + row.id)
            body.update(_automation_id=automation, _automation_scope='family')
            row.request = system[0].state.vault.seal(body, client.user_id + ':task:' + row.id)
        emit(db, Actor(client.user_id, 'h1', client.device_id, 'adult'), 'task.updated', identifier)
        db.commit()
    return identifier


def test_push_registration_secret_and_personal_inbox(system, alice):
    bob = SignedClient(system[1], system[2])
    registered = register(alice)
    assert registered.status_code == 200, registered.text
    assert registered.json()['status'] == 'not_configured'
    assert registered.json()['registered'] and not registered.json()['push_configured']
    identifier = completed_task(system, alice)
    collect_task_events(system[0].state, alice.user_id, 'h1')
    collect_task_events(system[0].state, alice.user_id, 'h1')
    page = alice.request('GET', '/api/v1/notifications').json()
    assert len(page['items']) == 1
    item = page['items'][0]
    assert item['task_id'] == identifier and item['scope'] == 'personal'
    assert '私人正文' not in json.dumps(page, ensure_ascii=False)
    assert bob.request('GET', '/api/v1/notifications').json()['items'] == []
    assert bob.request('GET', '/api/v1/notifications/' + item['id']).status_code == 404
    assert bob.request('POST', '/api/v1/notifications/' + item['id'] + '/read').status_code == 404
    first = alice.request('POST', '/api/v1/notifications/' + item['id'] + '/read').json()
    assert alice.request('POST', '/api/v1/notifications/' + item['id'] + '/read').json() == first
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        stored = db.scalar(select(PushRegistration))
        assert stored.token != 'ab' * 32
        assert system[0].state.vault.open(stored.token, alice.user_id + ':push:' + alice.device_id) == 'ab' * 32
        assert len(list(db.scalars(select(PushDelivery)))) == 1
    assert alice.request('DELETE', '/api/v1/devices/current/push').json() == {'registered': False}
    assert register(alice, 'no hex token ' * 4).status_code == 422


def family_automation(system, alice):
    identifier = str(uuid.uuid4())
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        db.add(Automation(id=identifier, owner_id=alice.user_id, household_id='h1', name='私人标题不进入推送',
                          cron='0 9 * * *', skill='encrypted-unused-in-test', enabled=True, next_run=now()+3600, visibility='family'))
        db.commit()
    return identifier


def test_family_notification_fanout_does_not_expose_private_task(system, alice):
    bob = SignedClient(system[1], system[2])
    outsider = SignedClient(system[1], system[2], household='h2')
    register(alice)
    register(bob, 'cd' * 32)
    automation = family_automation(system, alice)
    identifier = completed_task(system, alice, automation)
    collect_task_events(system[0].state, alice.user_id, 'h1')
    owner_item = alice.request('GET', '/api/v1/notifications').json()['items'][0]
    family_item = bob.request('GET', '/api/v1/notifications').json()['items'][0]
    assert owner_item['task_id'] == identifier
    assert family_item['task_id'] is None and family_item['conversation_id'] is None
    assert family_item['automation_id'] == automation and family_item['scope'] == 'family'
    assert outsider.request('GET', '/api/v1/notifications').json()['items'] == []
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        db.get(Automation, automation).visibility = 'personal'
        db.commit()
    assert bob.request('GET', '/api/v1/notifications').json()['items'] == []
    assert bob.request('GET', '/api/v1/notifications/' + family_item['id']).status_code == 404


def test_family_approval_only_notifies_executor(system, alice):
    bob = SignedClient(system[1], system[2])
    automation = family_automation(system, alice)
    completed_task(system, alice, automation, 'AWAITING_APPROVAL')
    collect_task_events(system[0].state, alice.user_id, 'h1')
    assert len(alice.request('GET', '/api/v1/notifications').json()['items']) == 1
    assert bob.request('GET', '/api/v1/notifications').json()['items'] == []


def test_due_reminder_runs_without_client_and_is_idempotent(system, alice):
    bob = SignedClient(system[1], system[2])
    due = datetime.fromtimestamp(now()-30, timezone.utc).isoformat()
    reminder = put(alice, record(kind='reminder.item', payload={'title': '私人提醒', 'due_at': due, 'notify_at_due': True}))
    future = put(alice, record(kind='reminder.item', source_id='future', payload={'title': '以后', 'due_at':datetime.fromtimestamp(now()+3600, timezone.utc).isoformat(), 'notify_at_due':True}))
    collect_due_reminders(system[0].state, alice.user_id, 'h1')
    collect_due_reminders(system[0].state, alice.user_id, 'h1')
    items = alice.request('GET', '/api/v1/notifications').json()['items']
    assert len(items) == 1 and items[0]['record_id'] == reminder and items[0]['status'] == 'DUE'
    assert bob.request('GET', '/api/v1/notifications').json()['items'] == []
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        row = db.get(Record, future)
        row.visibility = 'family'
        row.payload = system[0].state.vault.seal({'title': '家庭提醒', 'due_at':due, 'notify_at_due':True}, alice.user_id+':record:'+row.id)
        db.commit()
    collect_due_reminders(system[0].state, alice.user_id, 'h1')
    assert bob.request('GET', '/api/v1/notifications').json()['items'][0]['record_id'] == future


def configured(settings, tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    path = tmp_path / 'apns.p8'
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    path.chmod(0o600)
    settings.apns_key_file, settings.apns_key_id, settings.apns_team_id, settings.apns_topic = path, 'AAAAAAAAAA', 'BBBBBBBBBB', 'org.homeai.test'
    return key


@pytest.mark.asyncio
async def test_apns_protocol_and_revoked_devices(system, alice, tmp_path):
    settings = system[0].state.settings
    key = configured(settings, tmp_path)
    assert push_configuration(settings) == 'ready'
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(200)
    register(alice)
    completed_task(system, alice)
    collect_task_events(system[0].state, alice.user_id, 'h1')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        apns = APNs(settings, http)
        await deliver(system[0].state, apns, alice.user_id, 'h1')
        assert len(seen) == 1
        request = seen[0]
        assert request.url.host == 'api.sandbox.push.apple.com'
        payload = json.loads(request.content)
        assert set(payload) == {'aps', 'notification_id'}
        assert '私人' not in request.content.decode()
        header, claims, signature = request.headers['authorization'].removeprefix('bearer ').split('.')
        raw = base64.urlsafe_b64decode(signature + '==')
        key.public_key().verify(encode_dss_signature(int.from_bytes(raw[:32]), int.from_bytes(raw[32:])),
                                (header+'.'+claims).encode(), ec.ECDSA(hashes.SHA256()))
        assert request.headers['apns-push-type'] == 'alert'
        await deliver(system[0].state, apns, alice.user_id, 'h1')
        assert len(seen) == 1
        completed_task(system, alice)
        collect_task_events(system[0].state, alice.user_id, 'h1')
        with system[2]() as db:
            db.get(Device, alice.device_id).revoked = True
            db.commit()
        await deliver(system[0].state, apns, alice.user_id, 'h1')
        assert len(seen) == 1


@pytest.mark.asyncio
async def test_apns_transient_failure_and_invalid_token(system, alice, tmp_path):
    configured(system[0].state.settings, tmp_path)
    register(alice)
    completed_task(system, alice)
    collect_task_events(system[0].state, alice.user_id, 'h1')
    responses = [httpx.Response(503, json={'reason':'ServiceUnavailable'}), httpx.Response(410, json={'reason':'Unregistered'})]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: responses.pop(0))) as http:
        apns = APNs(system[0].state.settings, http)
        await deliver(system[0].state, apns, alice.user_id, 'h1')
        with system[2]() as db:
            scope(db, alice.user_id, 'h1')
            delivery = db.scalar(select(PushDelivery))
            assert delivery.status == 'retry' and delivery.retry_at > now()
            delivery.retry_at = 0
            db.commit()
        await deliver(system[0].state, apns, alice.user_id, 'h1')
        with system[2]() as db:
            scope(db, alice.user_id, 'h1')
            assert db.scalar(select(PushDelivery)).status == 'failed'
            assert not db.scalar(select(PushRegistration)).enabled


def test_stale_task_events_do_not_become_new_pushes_or_reserve_new_completion_key(system, alice):
    from homeai.db import Outbox
    register(alice)
    task_id = completed_task(system, alice)
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        event = db.scalar(select(Outbox).where(Outbox.resource_id == task_id, Outbox.kind == 'task.updated'))
        event.created_at = now() - 86401
        db.commit()
    collect_task_events(system[0].state, alice.user_id, 'h1')
    assert alice.request('GET', '/api/v1/notifications').json()['items'] == []
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        assert list(db.scalars(select(PushDelivery))) == []
        # 延迟到达的新完成事件必须仍可生成通知，不被旧事件占据幂等键。
        emit(db, Actor(alice.user_id, 'h1', alice.device_id, 'adult'), 'task.updated', task_id)
        db.flush()
        new_event = db.scalar(select(Outbox).where(Outbox.resource_id == task_id, Outbox.kind == 'task.updated').order_by(Outbox.id.desc()))
        new_event.created_at = now() - 30
        timestamp = new_event.created_at
        db.commit()
    collect_task_events(system[0].state, alice.user_id, 'h1')
    item = alice.request('GET', '/api/v1/notifications').json()['items'][0]
    assert item['created_at'] == timestamp
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        assert len(list(db.scalars(select(PushDelivery)))) == 1


def test_superseded_task_event_cannot_steal_latest_event_timestamp(system, alice):
    from homeai.db import Outbox
    task_id = completed_task(system, alice)
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        old_event = db.scalar(select(Outbox).where(Outbox.resource_id == task_id, Outbox.kind == 'task.updated'))
        old_event.created_at = now() - 3600
        emit(db, Actor(alice.user_id, 'h1', alice.device_id, 'adult'), 'task.updated', task_id)
        db.flush()
        new_event = db.scalar(select(Outbox).where(Outbox.resource_id == task_id, Outbox.kind == 'task.updated').order_by(Outbox.id.desc()))
        timestamp = new_event.created_at
        db.commit()
    collect_task_events(system[0].state, alice.user_id, 'h1')
    items = alice.request('GET', '/api/v1/notifications').json()['items']
    assert len(items) == 1 and items[0]['created_at'] == timestamp


def test_old_due_reminder_remains_history_without_new_push(system, alice):
    register(alice)
    due_at = now() - 172800
    put(alice, record(kind='reminder.item', payload={'title':'过去的提醒',
        'due_at':datetime.fromtimestamp(due_at, timezone.utc).isoformat(), 'notify_at_due':True}))
    collect_due_reminders(system[0].state, alice.user_id, 'h1')
    item = alice.request('GET', '/api/v1/notifications').json()['items'][0]
    assert item['created_at'] == pytest.approx(due_at)
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        assert list(db.scalars(select(PushDelivery))) == []
