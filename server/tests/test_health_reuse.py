"""健康复用使用隔离数据库和合成样本，不读取真实健康值。"""
from datetime import datetime, timedelta
import uuid

import pytest
from sqlalchemy import select

from conftest import SignedClient
from homeai.db import Invocation, Record, Task, now, scope
from homeai.health_reuse import inventory, narrow_pending_request, project_outcome, select_saved
from homeai.runtime import run_task
from test_client_actions import actor, new_task, request_action, response
from test_client_actions_health import execute_request, health_parameters, snapshot, upload


def sleep_snapshot(parameters):
    start = datetime.fromisoformat(parameters['start_at']) + timedelta(hours=2)
    return {**parameters, 'schema_version':2,'query_complete':True,'truncated':False,
        'status':'available','samples':[{'type':'sleep','sample_id':str(uuid.uuid4()),
        'start_at':start.isoformat(),'end_at':(start+timedelta(hours=3)).isoformat(),
        'value':3,'unit':'category'}]}


def test_saved_historical_sleep_does_not_expire_after_fifteen_minutes(system, alice):
    parameters = health_parameters(['sleep'])
    identifier = upload(alice, sleep_snapshot(parameters))
    with system[2]() as db:
        row = db.get(Record,identifier); row.updated_at = now()-86400; db.commit()
    result = execute_request(system,alice,new_task(system,alice),parameters)
    assert result['source']=='saved_authorized_server_record'
    assert result['health']['query_complete'] is True
    assert [row['id'] for row in result['records']]==[identifier]
    assert alice.request('GET','/api/v1/client-actions').json()['items']==[]


def test_recent_complete_capture_precedes_legacy_month_and_inventory_contains_no_values(system,alice):
    week=health_parameters(['sleep'])
    end=datetime.fromisoformat(week['end_at'])
    week={**week,'start_at':(end-timedelta(days=7)).isoformat()}
    month={**week,'start_at':(end-timedelta(days=30)).isoformat()}
    old=sleep_snapshot(month)
    for key in ('query_complete','truncated','schema_version'):old.pop(key)
    older_id=upload(alice,old)
    newer_id=upload(alice,sleep_snapshot(week))
    with system[2]() as db:
        rows,outcome=select_saved(system[0].state,db,actor(alice),{},month,alice.user_id)
        assert [row.id for row in rows]==[newer_id,older_id]
        assert outcome['health']['query_complete'] is None
        listings=inventory(system[0].state,db,actor(alice),{},types=['sleep'])
        assert len(listings)==2
        assert all(set(item)=={'types','start_at','end_at','query_complete','record_versions'} for item in listings)


def test_same_task_metric_subset_and_small_window_drift_do_not_ask_again(system,alice):
    parameters = health_parameters()
    task_id = new_task(system,alice)
    action_id = request_action(system,alice,task_id,'read_health',parameters)
    identifier = upload(alice,snapshot(parameters,['steps','heart_rate']))
    assert response(alice,action_id,record_ids=[identifier]).status_code==200
    changed = {**parameters,'types':['steps'],
        'end_at':(datetime.fromisoformat(parameters['end_at'])+timedelta(seconds=1)).isoformat()}
    result = execute_request(system,alice,task_id,changed)
    assert result['status']=='succeeded'
    assert result['health']['available_types']==['steps']
    assert set(result['health']['summary']['metrics'])=={'steps'}
    assert result['health']['time_range_complete'] is False
    assert len(alice.request('GET','/api/v1/client-actions').json()['items'])==1


@pytest.mark.parametrize('with_samples',[True,False])
def test_only_missing_metrics_are_requested_and_response_combines_existing_sleep(system,alice,with_samples):
    parameters = health_parameters(['sleep','heart_rate'])
    sleep_id = upload(alice,sleep_snapshot({**parameters,'types':['sleep']}))
    task_id = new_task(system,alice)
    pending = execute_request(system,alice,task_id,parameters)
    detail = alice.request('GET','/api/v1/client-actions/'+pending['client_action_id']).json()
    assert detail['parameters']['types']==['heart_rate']
    assert '已使用服务器保存的睡眠资料' in detail['purpose']
    health_id = upload(alice,{**snapshot(detail['parameters'],['heart_rate'] if with_samples else []),
        'schema_version':2,'query_complete':True,'truncated':False})
    accepted = response(alice,detail['id'],record_ids=[health_id])
    assert accepted.status_code==200,accepted.text
    reused = execute_request(system,alice,task_id,parameters)
    assert reused['status']=='succeeded'
    assert reused['health']['uncollected_types']==[]
    assert reused['health']['available_types']==(['sleep','heart_rate'] if with_samples else ['sleep'])
    assert reused['health']['missing_types']==([] if with_samples else ['heart_rate'])
    assert sleep_id in {row['id'] for row in reused['records']}
    assert len(alice.request('GET','/api/v1/client-actions').json()['items'])==1
    if not with_samples:
        assert health_id not in {row['id'] for row in reused['records']}


def test_partial_saved_window_does_not_claim_latest_or_entire_range(system,alice):
    parameters = health_parameters(['sleep'])
    upload(alice,sleep_snapshot(parameters))
    requested = {**parameters,
        'start_at':(datetime.fromisoformat(parameters['start_at'])-timedelta(days=6)).isoformat(),
        'end_at':(datetime.fromisoformat(parameters['end_at'])+timedelta(seconds=30)).isoformat()}
    result = execute_request(system,alice,new_task(system,alice),requested)
    assert result['status']=='succeeded' and result['data_status']=='partial'
    assert result['health']['time_range_complete'] is False
    assert result['health']['coverage_by_type']['sleep']['provided_ranges']==[
        {'start_at':parameters['start_at'],'end_at':parameters['end_at']}]


def test_sleep_subset_is_clipped_but_quantity_boundary_is_never_estimated(system,alice):
    parameters = health_parameters(['sleep','steps'])
    sleep = sleep_snapshot({**parameters,'types':['sleep']})['samples'][0]
    value = {**snapshot(parameters,['steps']), 'samples':[sleep,*snapshot(parameters,['steps'])['samples']]}
    identifier=upload(alice,value)
    subset={**parameters,'start_at':sleep['start_at'],'end_at':sleep['end_at']}
    with system[2]() as db:
        result=project_outcome(system[0].state,[db.get(Record,identifier)],subset)
    assert result['health']['available_types']==['sleep']
    assert result['health']['summary']['metrics']['sleep']['asleep_seconds']==10800
    assert '跨查询边界' in result['note']


def test_explicit_fresh_capture_is_not_silently_replaced_by_historical_data(system,alice):
    parameters=health_parameters(['sleep'])
    upload(alice,sleep_snapshot(parameters))
    task_id=new_task(system,alice)
    with system[2]() as db:
        task=db.get(Task,task_id)
        body=system[0].state.vault.open(task.request,alice.user_id+':task:'+task.id)
        body['message']='重新读取我的睡眠数据'
        task.request=system[0].state.vault.seal(body,alice.user_id+':task:'+task.id);db.commit()
    assert execute_request(system,alice,task_id,parameters)['status']=='waiting_client'


def test_reuse_never_reads_another_members_saved_health(system,alice):
    bob=SignedClient(system[1],system[2])
    parameters=health_parameters(['sleep'])
    upload(bob,sleep_snapshot(parameters))
    result=execute_request(system,alice,new_task(system,alice),parameters)
    assert result['status']=='waiting_client'
    with system[2]() as db:
        assert inventory(system[0].state,db,actor(alice),{})==[]


def test_changed_saved_source_rejects_supplement_without_consuming_request(system,alice):
    parameters=health_parameters(['sleep','heart_rate'])
    identifier=upload(alice,sleep_snapshot({**parameters,'types':['sleep']}))
    task_id=new_task(system,alice)
    pending=execute_request(system,alice,task_id,parameters)
    detail=alice.request('GET','/api/v1/client-actions/'+pending['client_action_id']).json()
    health_id=upload(alice,snapshot(detail['parameters'],['heart_rate']))
    with system[2]() as db:
        row=db.get(Record,identifier);row.version+=1;db.commit()
    rejected=response(alice,detail['id'],record_ids=[health_id])
    assert rejected.status_code==409
    assert alice.request('GET','/api/v1/client-actions/'+detail['id']).json()['status']=='PENDING'


async def test_existing_unanswered_full_request_can_be_narrowed_without_running_model(system,alice):
    parameters=health_parameters(['sleep','heart_rate'])
    created=alice.request('POST','/api/v1/tasks',{'idempotency_key':str(uuid.uuid4()),'message':'请整理健康报告',
        'steps':[{'capability':'client.request@v1','arguments':{'target_member_id':'self','kind':'read_health',
            'purpose':'整理健康报告','parameters':parameters}}]})
    task_id=created.json()['id']
    await run_task(system[0].state,task_id,alice.user_id)
    before=alice.request('GET','/api/v1/client-actions').json()['items'][0]
    upload(alice,sleep_snapshot({**parameters,'types':['sleep']}))
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        result=narrow_pending_request(system[0].state,db,actor(alice),task_id)
        assert result['changed'] and result['model_not_called']
        assert result['requested_types']==['heart_rate']
        db.commit()
    assert alice.request('GET','/api/v1/client-actions/'+before['id']).json()['status']=='CANCELED'
    detail=alice.request('GET','/api/v1/client-actions/'+result['client_action_id']).json()
    assert detail['parameters']['types']==['heart_rate']
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        assert narrow_pending_request(system[0].state,db,actor(alice),task_id)=={'changed':False}
        row=db.scalar(select(Invocation).where(Invocation.task_id==task_id))
        assert row.status=='WAITING_CLIENT' and row.result is None
