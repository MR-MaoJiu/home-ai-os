"""真实 PostgreSQL、OPA 与 JetStream 的事件工作流，不替换任何业务执行结果。"""
import json
import os
import uuid
import nats
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from homeai.api import create_app
from homeai.config import Settings
from homeai.db import Outbox, AutomationDelivery, Automation, Task, Device, Principal, scope
from homeai.event_automations import consume, dispatch, subscription, drain
from homeai.runtime import run_task
from conftest import SignedClient

pytestmark = pytest.mark.skipif(os.getenv('HOMEAI_INTEGRATION') != '1', reason='需要真实 PostgreSQL、OPA 和 NATS')


@pytest.fixture
def workflow():
    settings = Settings()
    settings.database_url = settings.database_url.rsplit('/', 1)[0] + '/homeai_test'
    suffix = uuid.uuid4().hex
    settings.event_stream = 'AUTOTEST_' + suffix
    settings.event_subject_prefix = 'homeai.test.' + suffix
    app = create_app(settings)
    user = SignedClient(TestClient(app), app.state.db, household=str(uuid.uuid4()))
    return app.state, user


def rule(user, **options):
    body = {'name': '资料更新提醒', 'trigger_kind': 'event', 'event_type': 'record.changed',
        'enabled': True, 'skill': {'name': '更新通知', 'steps': [{'capability': 'reminder.create@v1',
        'arguments': {'title': '资料有变化', 'source_record': {'$event': 'record_id'}}}]}, **options}
    result = user.request('POST', '/api/v1/automations', body)
    assert result.status_code == 200, result.text
    return result.json()['id']


def record(user, **options):
    item = {'source': 'event-test', 'source_id': str(uuid.uuid4()), 'kind': 'note', 'version': 1,
        'payload': {'title': '真实写入测试记录'}, **options}
    result = user.request('POST', '/api/v1/data/sync', {'records': [item]})
    assert result.status_code == 200, result.text
    return result.json()['records'][0]['id']


def events(app, user, rid):
    with app.db() as db:
        principal = db.get(Principal, user.user_id)
        scope(db, user.user_id, principal.household_id)
        return list(db.scalars(select(Outbox).where(Outbox.resource_id == rid, Outbox.owner_id == user.user_id, Outbox.kind == 'record.changed')))


def deliver(app, event):
    consume(app, {'event_id': event.event_id, 'owner_id': event.owner_id}, app.settings.event_subject_prefix + '.' + event.kind)


def dispatch_user(app, user):
    with app.db() as db:
        principal = db.get(Principal, user.user_id)
        dispatch(app, user.user_id, principal.household_id, principal.role)


def deliveries(app, user, aid):
    with app.db() as db:
        principal = db.get(Principal, user.user_id)
        scope(db, user.user_id, principal.household_id)
        return list(db.scalars(select(AutomationDelivery).where(AutomationDelivery.automation_id == aid).order_by(AutomationDelivery.created_at)))


@pytest.mark.asyncio
async def test_real_jetstream_duplicate_restart_and_loop(workflow):
    app, user = workflow
    aid = rule(user)
    rid = record(user)
    event = events(app, user, rid)[0]
    nc = await nats.connect(app.settings.nats_url)
    js = nc.jetstream()
    await js.add_stream(name=app.settings.event_stream, subjects=[app.settings.event_subject_prefix + '.>'])
    try:
        sub = await subscription(app, js)
        body = json.dumps({'event_id': event.event_id, 'owner_id': user.user_id}).encode()
        await js.publish(app.settings.event_subject_prefix + '.record.changed', body)
        # 持久化后但未 ACK 断开，再次订阅重投；不伪造消息或成功响应。
        msg = (await sub.fetch(1, timeout=2))[0]
        consume(app, json.loads(msg.data), msg.subject)
        await msg.nak()
        await sub.unsubscribe()
        sub = await subscription(app, js)
        await drain(app, sub)
        await js.publish(app.settings.event_subject_prefix + '.record.changed', body)
        await drain(app, sub)
        dispatch_user(app, user)
        rows = deliveries(app, user, aid)
        assert len(rows) == 1 and rows[0].status == 'DISPATCHED'
        tid = rows[0].task_id
        await run_task(app, tid, user.user_id)
        task = user.request('GET', '/api/v1/tasks/' + tid).json()
        assert task['status'] == 'SUCCEEDED', task
        reminder_id = task['result']['record_id']
        reminder = user.request('GET', '/api/v1/data/' + reminder_id).json()
        assert reminder['payload']['source_record'] == rid
        output = events(app, user, reminder_id)[0]
        assert json.loads(output.automation_chain) == [aid]
        deliver(app, output)
        dispatch_user(app, user)
        rows = deliveries(app, user, aid)
        assert [row.status for row in rows] == ['DISPATCHED', 'SKIPPED']
    finally:
        await js.delete_stream(app.settings.event_stream)
        await nc.drain()


def test_cooldown_queues_disable_cancels_and_forgery_rejected(workflow):
    app, user = workflow
    aid = rule(user, cooldown_seconds=60)
    rid1, rid2 = record(user), record(user)
    first, second = events(app, user, rid1)[0], events(app, user, rid2)[0]
    consume(app, {'event_id': first.event_id, 'owner_id': str(uuid.uuid4())}, app.settings.event_subject_prefix + '.record.changed')
    consume(app, {'event_id': first.event_id, 'owner_id': user.user_id}, app.settings.event_subject_prefix + '.record.deleted')
    assert deliveries(app, user, aid) == []
    deliver(app, first)
    deliver(app, second)
    dispatch_user(app, user)
    assert [r.status for r in deliveries(app, user, aid)] == ['DISPATCHED', 'PENDING']
    assert user.request('DELETE', '/api/v1/automations/' + aid).status_code == 200
    assert [r.status for r in deliveries(app, user, aid)] == ['DISPATCHED', 'CANCELED']


@pytest.mark.asyncio
async def test_deleted_source_and_revoked_device_never_execute(workflow):
    app, user = workflow
    aid = rule(user)
    rid = record(user)
    deliver(app, events(app, user, rid)[0])
    assert user.request('DELETE', '/api/v1/data/' + rid).status_code == 200
    dispatch_user(app, user)
    assert deliveries(app, user, aid)[0].status == 'SKIPPED'
    rid = record(user)
    deliver(app, events(app, user, rid)[0])
    dispatch_user(app, user)
    tid = deliveries(app, user, aid)[1].task_id
    assert user.request('DELETE', '/api/v1/data/' + rid).status_code == 200
    await run_task(app, tid, user.user_id)
    with app.db() as db:
        principal = db.get(Principal, user.user_id)
        scope(db, user.user_id, principal.household_id)
        assert db.get(Task, tid).status == 'FAILED'
    rid = record(user)
    deliver(app, events(app, user, rid)[0])
    with app.db() as db:
        db.get(Device, user.device_id).revoked = True
        db.commit()
    dispatch_user(app, user)
    assert deliveries(app, user, aid)[2].status == 'CANCELED'


def test_shared_opt_in_revocation_and_cross_owner_delivery_isolation(workflow):
    app, alice = workflow
    with app.db() as db:
        household = db.get(Principal, alice.user_id).household_id
    bob = SignedClient(alice.client, app.db, household=household)
    private_rule = rule(bob)
    shared_rule = rule(bob, include_shared=True)
    rid = record(alice)
    assert alice.request('PUT', f'/api/v1/data/{rid}/grants/{bob.user_id}').status_code == 200
    event = events(app, bob, rid)[0]
    deliver(app, event)
    assert deliveries(app, bob, private_rule) == []
    assert len(deliveries(app, bob, shared_rule)) == 1
    assert alice.request('GET', f'/api/v1/automations/{shared_rule}/deliveries').status_code == 404
    assert alice.request('DELETE', f'/api/v1/data/{rid}/grants/{bob.user_id}').status_code == 200
    dispatch_user(app, bob)
    assert deliveries(app, bob, shared_rule)[0].status == 'SKIPPED'


def test_concurrent_consumption_and_dispatch_are_unique(workflow):
    from concurrent.futures import ThreadPoolExecutor
    app, user = workflow
    aid = rule(user)
    rid = record(user)
    event = events(app, user, rid)[0]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: deliver(app, event), range(8)))
        list(pool.map(lambda _: dispatch_user(app, user), range(8)))
    rows = deliveries(app, user, aid)
    assert len(rows) == 1 and rows[0].status == 'DISPATCHED'
    assert user.request('GET', f'/api/v1/automations/{aid}/deliveries').json()[0]['task_id'] == rows[0].task_id
