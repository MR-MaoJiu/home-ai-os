"""只有完整费用证据才校正旧任务预留；缺失证据和未知调用不能被免费清空。"""
import pytest
from homeai.db import Task, uid, now, scope
from homeai.security import Actor
from homeai.model_routing import ModelUsage
from homeai.token_accounting import reconcile_cloud_counters, reserve_planning, settle_planning


@pytest.mark.parametrize('complete', [True, False])
def test_legacy_cloud_plan_counters_require_complete_round_coverage(system, alice, complete):
    actor = Actor(alice.user_id, 'h1', alice.device_id, 'infrastructure_owner')
    with system[2]() as db:
        scope(db, actor.user_id, actor.household_id)
        task = Task(id=uid(),owner_id=actor.user_id,household_id='h1',request='test',request_hash='test',idempotency_key=uid(),deadline=now()+60)
        db.add(task); db.flush()
        for index, amount in enumerate((7092,7894)):
            db.add(ModelUsage(id=uid(),owner_id=actor.user_id,household_id='h1',task_id=task.id,
                call_key=task.id+':plan:'+str(index),day='2026-09-30',reserved=27000,charged=amount,status='SUCCEEDED'))
        body={'_model_rounds':2 if complete else 3,'_cloud_calls':2,'_model_token_charge':34604,'_cloud_token_charge':33696}
        changes=reconcile_cloud_counters(db,actor,task,body)
        assert body['_cloud_token_charge']==14986
        assert changes['_cloud_token_charge']==(33696,14986)
        assert body['_model_token_charge']==(14986 if complete else 34604)
        again=reconcile_cloud_counters(db,actor,task,body)
        assert not again


def test_unknown_cloud_charge_is_kept_and_missing_cloud_calls_are_not_guessed(system,alice):
    actor=Actor(alice.user_id,'h1',alice.device_id,'infrastructure_owner')
    with system[2]() as db:
        scope(db,actor.user_id,actor.household_id)
        task=Task(id=uid(),owner_id=actor.user_id,household_id='h1',request='test',request_hash='test',idempotency_key=uid(),deadline=now()+60)
        db.add(task);db.flush()
        db.add(ModelUsage(id=uid(),owner_id=actor.user_id,household_id='h1',task_id=task.id,call_key=task.id+':plan:0',day='2026-09-30',reserved=12000,charged=12000,status='UNKNOWN'))
        body={'_model_rounds':1,'_cloud_calls':1,'_model_token_charge':12000,'_cloud_token_charge':12000}
        reconcile_cloud_counters(db,actor,task,body)
        assert body['_model_token_charge']==body['_cloud_token_charge']==12000
        assert body['_planning_usage'][task.id+':plan:0']['status']=='UNKNOWN'
        incomplete={'_model_rounds':2,'_cloud_calls':2,'_model_token_charge':24000,'_cloud_token_charge':24000}
        assert not reconcile_cloud_counters(db,actor,task,incomplete)
        assert incomplete['_cloud_token_charge']==24000


def test_settlement_is_idempotent_and_preserves_prior_rounds():
    body={'_model_token_charge':500}
    reserve_planning(body,'next',20000)
    assert body['_model_token_charge']==20500
    settle_planning(body,'next',{'usage':{'total_tokens':7894}})
    settle_planning(body,'next',{'usage':{'total_tokens':7894}})
    assert body['_model_token_charge']==8394
    reserve_planning(body,'larger',100)
    settle_planning(body,'larger',{'usage':{'total_tokens':1000}})
    assert body['_model_token_charge']==9394
    reserve_planning(body,'missing',200)
    settle_planning(body,'missing',{'choices':[]})
    assert body['_model_token_charge']==9594
    assert body['_planning_usage']['missing']['status']=='ESTIMATED'
