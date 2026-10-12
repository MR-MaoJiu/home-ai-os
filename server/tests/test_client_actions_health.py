"""真实服务端请求与签名响应链路；隔离数据库只使用人工构造的非个人健康样本。"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from homeai.client_actions import execute
from homeai.db import Invocation, Notification, Outbox, Record, Task, scope
from homeai.runtime import run_task
from test_client_actions import actor, new_task, request_action, response
from test_security_data import put, record


def health_parameters(types=None):
    end = datetime.now(timezone.utc) - timedelta(minutes=1)
    return {'types': types or ['steps', 'heart_rate'],
        'start_at': (end - timedelta(days=1)).isoformat(), 'end_at': end.isoformat()}


def snapshot(parameters, types=()):
    samples = [{'type': kind, 'sample_id': str(uuid.uuid4()),
        'start_at': parameters['start_at'], 'end_at': parameters['end_at'],
        'value': 100 if kind == 'steps' else 60,
        'unit': 'count' if kind == 'steps' else 'count/min'} for kind in types]
    return {**parameters, 'samples': samples, 'status': 'available' if samples else 'no_data'}


def upload(client, value):
    return put(client, record(source_id=str(uuid.uuid4()), kind='health.snapshot', payload=value))


def execute_request(system, client, task_id, parameters):
    with system[2]() as db:
        scope(db, client.user_id, 'h1')
        task = db.get(Task, task_id)
        result = execute(system[0].state, db, actor(client), task, None, 'client.request@v1', {
            'target_member_id': 'self', 'kind': 'read_health',
            'purpose': '读取所选时间范围的健康记录', 'parameters': parameters})
        db.commit()
        return result


def test_empty_health_response_resumes_original_workflow_without_claiming_samples(system, alice):
    parameters = health_parameters()
    created = alice.request('POST', '/api/v1/tasks', {'idempotency_key': str(uuid.uuid4()),
        'message': '隔离测试健康采集与任务恢复', 'steps': [
            {'capability': 'client.request@v1', 'arguments': {'target_member_id': 'self',
                'kind': 'read_health', 'purpose': '读取所选范围', 'parameters': parameters}},
            {'capability': 'memory.search@v1', 'arguments': {'query': '无匹配的测试资料'}},
        ]})
    assert created.status_code == 202, created.text
    task_id = created.json()['id']
    asyncio.run(run_task(system[0].state, task_id, alice.user_id))
    assert alice.request('GET', '/api/v1/tasks/' + task_id).json()['status'] == 'WAITING_CLIENT'
    action = alice.request('GET', '/api/v1/client-actions').json()['items'][0]
    identifier = upload(alice, snapshot(parameters))
    body = {'idempotency_key': str(uuid.uuid4()), 'record_ids': [identifier]}
    accepted = alice.request('POST', '/api/v1/client-actions/' + action['id'] + '/respond', body)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['status'] == 'RESPONDED'
    assert accepted.json()['data_status'] == 'no_data'
    assert accepted.json()['record_ids'] == []
    assert '无法据此确定是否拒绝授权' in accepted.json()['note']
    assert alice.request('POST', '/api/v1/client-actions/' + action['id'] + '/respond', body).json() == accepted.json()
    assert alice.request('GET', '/api/v1/client-actions/' + action['id']).json()['data_status'] == 'no_data'
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, task_id)
        payload = system[0].state.vault.open(task.request, alice.user_id + ':task:' + task_id)
        assert task.status == 'RECEIVED' and task.error is None
        assert identifier not in payload.get('record_ids', [])
        assert identifier not in payload.get('_record_dependencies', {})
        assert '_client_wait' not in payload
        invocation = db.scalar(select(Invocation).where(Invocation.task_id == task_id, Invocation.step == 0))
        result = system[0].state.vault.open(invocation.result, alice.user_id + ':invocation-result:' + invocation.id)
        assert invocation.status == 'SUCCEEDED' and result['status'] == 'no_data'
        assert result['health']['sample_count'] == 0
        notification = db.get(Notification, action['notification_id'])
        assert notification.status == 'RESPONDED'
        assert db.scalar(select(func.count()).select_from(Outbox).where(
            Outbox.kind == 'client.action.updated', Outbox.resource_id == action['id'])) == 1
    # Worker 在原任务继续后续步骤，不需要用户重发消息，也不新增健康授权卡片。
    asyncio.run(run_task(system[0].state, task_id, alice.user_id))
    asyncio.run(run_task(system[0].state, task_id, alice.user_id))
    assert alice.request('GET', '/api/v1/tasks/' + task_id).json()['status'] == 'SUCCEEDED'
    assert len(alice.request('GET', '/api/v1/client-actions').json()['items']) == 1


@pytest.mark.parametrize(('types', 'expected'), [(['steps'], 'partial'), (['steps', 'heart_rate'], 'available')])
def test_health_response_reports_actual_availability_and_retains_data_channel(system, alice, types, expected):
    parameters = health_parameters()
    task_id = new_task(system, alice)
    action_id = request_action(system, alice, task_id, 'read_health', parameters)
    identifier = upload(alice, snapshot(parameters, types))
    accepted = response(alice, action_id, record_ids=[identifier])
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['data_status'] == expected
    detail = alice.request('GET', '/api/v1/client-actions/' + action_id).json()
    assert detail['data_status'] == expected
    assert alice.request('GET', '/api/v1/tasks/' + task_id).json()['status'] == 'RECEIVED'
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        row = db.get(Record, identifier)
        assert row.kind == 'health.snapshot'
        task = db.get(Task, task_id)
        payload = system[0].state.vault.open(task.request, alice.user_id + ':task:' + task_id)
        assert identifier in payload['record_ids']
    reused = execute_request(system, alice, task_id, parameters)
    assert reused['data_status'] == expected
    assert reused['health']['sample_count'] == len(types)
    assert reused['health']['available_types'] == types
    assert reused['health']['missing_types'] == [kind for kind in parameters['types'] if kind not in types]
    assert len(alice.request('GET', '/api/v1/client-actions').json()['items']) == 1


def test_empty_server_snapshot_does_not_suppress_new_health_permission_request(system, alice):
    parameters = health_parameters()
    upload(alice, snapshot(parameters))
    task_id = new_task(system, alice)
    result = execute_request(system, alice, task_id, parameters)
    assert result['status'] == 'waiting_client'
    assert alice.request('GET', '/api/v1/tasks/' + task_id).json()['status'] == 'WAITING_CLIENT'


def test_same_task_empty_response_is_not_repeated_but_new_task_can_collect_again(system, alice):
    parameters = health_parameters()
    task_id = new_task(system, alice)
    action_id = request_action(system, alice, task_id, 'read_health', parameters)
    identifier = upload(alice, snapshot(parameters))
    assert response(alice, action_id, record_ids=[identifier]).status_code == 200
    reused = execute_request(system, alice, task_id, {**parameters, 'types': list(reversed(parameters['types']))})
    assert reused['data_status'] == 'no_data'
    assert reused['records'] == [] and reused['client_action_id'] == action_id
    assert len(alice.request('GET', '/api/v1/client-actions').json()['items']) == 1
    second = execute_request(system, alice, new_task(system, alice), parameters)
    assert second['status'] == 'waiting_client' and second['client_action_id'] != action_id


def test_available_server_snapshot_can_be_used_without_reopening_authorization(system, alice):
    parameters = health_parameters()
    identifier = upload(alice, snapshot(parameters, ['steps', 'heart_rate']))
    result = execute_request(system, alice, new_task(system, alice), parameters)
    assert result['source'] == 'saved_authorized_server_record'
    assert result['data_status'] == 'available'
    assert result['records'][0]['id'] == identifier
    assert alice.request('GET', '/api/v1/client-actions').json()['items'] == []


def test_invalid_health_snapshot_does_not_consume_request_or_resume_task(system, alice):
    parameters = health_parameters()
    task_id = new_task(system, alice)
    action_id = request_action(system, alice, task_id, 'read_health', parameters)
    identifier = upload(alice, {**snapshot(parameters, ['steps']), 'types': ['steps']})
    rejected = response(alice, action_id, record_ids=[identifier])
    assert rejected.status_code == 422
    assert alice.request('GET', '/api/v1/client-actions/' + action_id).json()['status'] == 'PENDING'
    assert alice.request('GET', '/api/v1/tasks/' + task_id).json()['status'] == 'WAITING_CLIENT'


@pytest.mark.parametrize('cached_days', [1, 8])
def test_saved_health_window_is_reused_only_as_actual_partial_data(system, alice, cached_days):
    parameters = health_parameters()
    end = datetime.fromisoformat(parameters['end_at'])
    requested = {**parameters, 'start_at': (end - timedelta(days=7)).isoformat()}
    cached = {**parameters, 'start_at': (end - timedelta(days=cached_days)).isoformat()}
    upload(alice, snapshot(cached, ['steps', 'heart_rate']))
    result = execute_request(system, alice, new_task(system, alice), requested)
    if cached_days == 1:
        assert result['status'] == 'succeeded' and result['data_status'] == 'partial'
        assert result['health']['time_range_complete'] is False
        assert result['health']['provided_ranges'] == [{'start_at':cached['start_at'],'end_at':cached['end_at']}]
    else:
        # 合成计量样本横跨八天，不能按比例猜测其中七天的步数或心率。
        assert result['status'] == 'waiting_client'
        assert 'records' not in result


def test_shorter_client_health_window_is_partial_and_original_task_continues(system, alice):
    parameters = health_parameters()
    end = datetime.fromisoformat(parameters['end_at'])
    requested = {**parameters, 'start_at': (end - timedelta(days=7)).isoformat()}
    task_id = new_task(system, alice)
    action_id = request_action(system, alice, task_id, 'read_health', requested)
    identifier = upload(alice, snapshot(parameters, ['steps', 'heart_rate']))
    accepted = response(alice, action_id, record_ids=[identifier])
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['data_status'] == 'partial'
    assert '时间范围未完整覆盖请求' in accepted.json()['note']
    assert alice.request('GET', '/api/v1/tasks/' + task_id).json()['status'] == 'RECEIVED'
    reused = execute_request(system, alice, task_id, requested)
    assert reused['data_status'] == 'partial'
    assert reused['health']['missing_types'] == []
    assert reused['health']['time_range_complete'] is False
    assert reused['health']['provided_ranges'] == [{
        'start_at': parameters['start_at'], 'end_at': parameters['end_at']}]
    assert reused['health']['requested_start_at'] == requested['start_at']


def test_health_ranges_with_gap_are_not_reported_as_complete(system, alice):
    parameters = health_parameters()
    start = datetime.fromisoformat(parameters['start_at'])
    first = {**parameters, 'end_at': (start + timedelta(hours=8)).isoformat()}
    second = {**parameters, 'start_at': (start + timedelta(hours=16)).isoformat()}
    task_id = new_task(system, alice)
    action_id = request_action(system, alice, task_id, 'read_health', parameters)
    identifiers = [upload(alice, snapshot(value, ['steps', 'heart_rate'])) for value in (first, second)]
    accepted = response(alice, action_id, record_ids=identifiers)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['data_status'] == 'partial'
    outcome = execute_request(system, alice, task_id, parameters)
    assert outcome['health']['time_range_complete'] is False


def test_health_response_changes_other_device_notification_revision_without_new_notice(system, alice):
    import copy
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    from conftest import SignedClient
    from homeai.crypto import digest
    from homeai.security import credential
    from homeai.task_events import snapshot as state_snapshot

    # 同一成员的第二台设备真实配对并拥有独立签名私钥和会话。
    second = copy.copy(alice)
    second.key = ec.generate_private_key(ec.SECP256R1())
    with system[2]() as db:
        token = credential(db, alice.user_id, 'pair', 300)
        db.commit()
    paired = system[1].post('/api/v1/pair', json={'token': token, 'name': '第二台测试设备',
        'public_key': second.key.public_key().public_bytes(serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
        'signature': second.sign(('homeai-pair:' + token).encode())})
    assert paired.status_code == 200, paired.text
    second.device_id = paired.json()['device_id']
    second.token = paired.json()['access_token']
    outsider = SignedClient(system[1], system[2])
    parameters = health_parameters()
    task_id = new_task(system, alice)
    action_id = request_action(system, alice, task_id, 'read_health', parameters)
    identifier = upload(alice, snapshot(parameters, ['steps']))
    before = state_snapshot(system[0].state, actor(second), digest(second.token.encode()), None)
    outsider_before = state_snapshot(system[0].state, actor(outsider), digest(outsider.token.encode()), None)
    notices_before = second.request('GET', '/api/v1/notifications').json()['items']
    assert second.request('GET', '/api/v1/client-actions/' + action_id).json()['status'] == 'PENDING'
    accepted = response(alice, action_id, record_ids=[identifier])
    assert accepted.status_code == 200, accepted.text
    after = state_snapshot(system[0].state, actor(second), digest(second.token.encode()), None)
    assert after['notification_revision'] != before['notification_revision']
    notices_after = second.request('GET', '/api/v1/notifications').json()['items']
    assert [item['id'] for item in notices_after] == [item['id'] for item in notices_before]
    assert notices_after[0]['status'] == 'RESPONDED'
    assert second.request('GET', '/api/v1/client-actions/' + action_id).json()['data_status'] == 'partial'
    outsider_after = state_snapshot(system[0].state, actor(outsider), digest(outsider.token.encode()), None)
    assert outsider_after['notification_revision'] == outsider_before['notification_revision']


def test_health_chunks_resume_with_complete_summary_but_partial_upload_does_not_fake_complete(system, alice):
    parameters = health_parameters(['steps'])
    capture = str(uuid.uuid4())
    first = {**snapshot(parameters,['steps']),'schema_version':2,'capture_id':capture,
        'chunk_index':0,'chunk_count':2,'query_complete':True,'truncated':False}
    second = {**snapshot(parameters,['steps']),**{key:first[key] for key in
        ('schema_version','capture_id','chunk_count','query_complete','truncated')},'chunk_index':1}
    first_id = upload(alice,first)
    task_id = new_task(system,alice)
    pending = execute_request(system,alice,task_id,parameters)
    assert pending['status'] == 'waiting_client'
    accepted = response(alice,pending['client_action_id'],record_ids=[first_id])
    assert accepted.json()['data_status'] == 'partial'
    outcome = execute_request(system,alice,task_id,parameters)
    assert outcome['health']['query_complete'] is False
    assert outcome['health']['summary']['sample_count'] == 1
    second_id = upload(alice,second)
    complete = execute_request(system,alice,new_task(system,alice),parameters)
    assert complete['source'] == 'saved_authorized_server_record'
    assert complete['health']['time_range_complete'] is True
    assert complete['health']['summary']['sample_count'] == 2
    assert set(row['id'] for row in complete['records']) == {first_id,second_id}


def test_health_repeated_sample_id_cannot_replace_value_across_uploaded_records(system,alice):
    parameters = health_parameters(['steps'])
    first = snapshot(parameters,['steps'])
    second = {**first,'samples':[{**first['samples'][0],'value':999}]}
    task_id = new_task(system,alice)
    action_id = request_action(system,alice,task_id,'read_health',parameters)
    result = response(alice,action_id,record_ids=[upload(alice,first),upload(alice,second)])
    assert result.status_code == 422
    assert alice.request('GET','/api/v1/client-actions/'+action_id).json()['status'] == 'PENDING'


async def test_received_sleep_task_reads_full_summary_after_resuming_without_raw_sample_prompt(system,alice):
    import json
    from test_task_clarification import setup_planner, tool
    from test_health_plan_recovery import new_task
    parameters = health_parameters(['sleep'])
    calls,_ = setup_planner(system,[{'tool_calls':[tool('request_client_data',{
        'target_member_id':'self','kind':'read_health','purpose':'分析本次睡眠','parameters':parameters})]},
        {'content':'已按提供的睡眠记录完成统计。'}])
    task_id = new_task(alice,'请分析最近一天的睡眠记录。')
    for _ in range(3):
        await run_task(system[0].state,task_id,alice.user_id)
        if alice.request('GET','/api/v1/tasks/'+task_id).json()['status']=='WAITING_CLIENT':break
    action = alice.request('GET','/api/v1/client-actions').json()['items'][0]
    begin = datetime.fromisoformat(parameters['start_at'])+timedelta(hours=1)
    end = begin+timedelta(hours=2)
    rows = [{'type':'sleep','sample_id':'test-'+str(index),'start_at':begin.isoformat(),'end_at':end.isoformat(),
        'value':3,'unit':'category'} for index in range(723)]
    identifier = upload(alice,{**parameters,'status':'available','samples':rows})
    assert response(alice,action['id'],record_ids=[identifier]).status_code==200
    for _ in range(5):
        await run_task(system[0].state,task_id,alice.user_id)
        if alice.request('GET','/api/v1/tasks/'+task_id).json()['status']=='SUCCEEDED':break
    assert alice.request('GET','/api/v1/tasks/'+task_id).json()['status']=='SUCCEEDED'
    receipts = [json.loads(message['content']) for call in calls for message in call['messages'] if message['role']=='tool']
    health = receipts[-1]['health']
    assert health['query_complete'] is None
    assert health['summary']['sample_count']==723
    assert health['summary']['metrics']['sleep']['asleep_seconds']==7200
    assert health['summary']['metrics']['sleep']['stage_minutes']['asleep_core']==120
    assert all('samples' not in record for record in receipts[-1]['records'])
    assert 'test-722' not in json.dumps(calls,ensure_ascii=False)
