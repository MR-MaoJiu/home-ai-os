"""真实 PostgreSQL/OPA：服务端执行提醒、离线到期通知、RLS 和重启去重。"""
import os
import uuid
from datetime import datetime, timezone
import pytest
from sqlalchemy import select
from conftest import SignedClient
from test_workflows import workflow
from homeai.db import Notification, PushDelivery, scope
from homeai.notifications import collect_due_reminders, collect_task_events
from homeai.runtime import run_task

pytestmark = pytest.mark.skipif(os.environ.get('HOMEAI_INTEGRATION') != '1', reason='需要真实 PostgreSQL 和 OPA')


@pytest.mark.asyncio
async def test_real_task_and_due_notification_are_owner_scoped(workflow):
    app, user = workflow
    household = None
    with app.db() as db:
        from homeai.db import Principal
        household = db.get(Principal, user.user_id).household_id
    another = SignedClient(user.client, app.db, household=household)
    token_registration = user.request('PUT', '/api/v1/devices/current/push', {
        'token': 'ab'*32, 'environment':'sandbox', 'authorization':'authorized'})
    assert token_registration.status_code == 200
    due = datetime.now(timezone.utc).isoformat()
    result = user.request('POST', '/api/v1/tasks', {'idempotency_key':str(uuid.uuid4()),
        'steps':[{'capability':'reminder.create@v1','arguments':{
            'title':'隔离测试提醒，不触发 Apple 网络', 'due_at':due, 'notify_at_due':True}}]})
    assert result.status_code == 202, result.text
    identifier = result.json()['id']
    await run_task(app, identifier, user.user_id)
    assert user.request('GET','/api/v1/tasks/'+identifier).json()['status'] == 'SUCCEEDED'
    collect_task_events(app, user.user_id, household)
    collect_due_reminders(app, user.user_id, household)
    # 重建消费者时只读取 PostgreSQL 状态，不依赖内存已发送集合。
    collect_task_events(app, user.user_id, household)
    collect_due_reminders(app, user.user_id, household)
    items = user.request('GET','/api/v1/notifications').json()['items']
    assert {item['kind'] for item in items} == {'task.updated', 'reminder.due'}
    assert len(items) == 2
    assert another.request('GET','/api/v1/notifications').json()['items'] == []
    with app.db() as db:
        scope(db, another.user_id, household)
        assert list(db.scalars(select(Notification).where(Notification.owner_id == user.user_id))) == []
        assert list(db.scalars(select(PushDelivery).where(PushDelivery.owner_id == user.user_id))) == []
    assert user.request('DELETE','/api/v1/devices/current/push').status_code == 200


@pytest.mark.asyncio
async def test_real_pending_approval_event_is_in_inbox(workflow):
    app, user = workflow
    from homeai.db import Principal
    with app.db() as db:
        household = db.get(Principal, user.user_id).household_id
    response = user.request('POST', '/api/v1/tasks', {'idempotency_key':str(uuid.uuid4()),
        'steps':[{'capability':'mail.send@v1','arguments':{'to':'test@example.com','subject':'不会发送','text':'只到审批阶段'}}]})
    identifier = response.json()['id']
    await run_task(app, identifier, user.user_id)
    assert user.request('GET','/api/v1/tasks/'+identifier).json()['status'] == 'AWAITING_APPROVAL'
    collect_task_events(app, user.user_id, household)
    assert user.request('GET','/api/v1/notifications').json()['items'][0]['status'] == 'AWAITING_APPROVAL'
    assert user.request('POST','/api/v1/tasks/'+identifier+'/cancel').status_code == 200


@pytest.mark.asyncio
async def test_real_family_reminder_notifies_members_but_no_private_body(workflow):
    app, user = workflow
    from homeai.db import Principal, Record
    with app.db() as db:
        household = db.get(Principal, user.user_id).household_id
    member = SignedClient(user.client, app.db, household=household)
    outside = SignedClient(user.client, app.db, household=str(uuid.uuid4()))
    response = user.request('POST', '/api/v1/tasks', {'idempotency_key':str(uuid.uuid4()),
        'steps':[{'capability':'reminder.create@v1','arguments':{
            'title':'家庭到期提醒测试','due_at':datetime.now(timezone.utc).isoformat(),'notify_at_due':True}}]})
    identifier = response.json()['id']
    await run_task(app, identifier, user.user_id)
    task = user.request('GET','/api/v1/tasks/'+identifier).json()
    assert task['status'] == 'SUCCEEDED'
    rid = task['result']['record_id']
    with app.db() as db:
        scope(db, user.user_id, household)
        db.get(Record, rid).visibility = 'family'
        db.commit()
    collect_due_reminders(app, user.user_id, household)
    item = member.request('GET','/api/v1/notifications').json()['items'][0]
    assert item['scope'] == 'family' and item['record_id'] == rid and item['task_id'] is None
    assert outside.request('GET','/api/v1/notifications').json()['items'] == []
    with app.db() as db:
        scope(db, user.user_id, household)
        db.get(Record, rid).visibility = 'personal'
        db.commit()
    assert member.request('GET','/api/v1/notifications/'+item['id']).status_code == 404
