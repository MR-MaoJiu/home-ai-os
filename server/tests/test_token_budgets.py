"""每日 Token 额度的结构化错误和派发幂等边界，仅使用隔离测试账本。"""
from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from conftest import SignedClient
from homeai.db import scope
from homeai.model_routing import BudgetExceeded, ModelUsage, reserve
from homeai.security import Actor


def limits(alice, household=100, member=100):
    response=alice.request('PUT','/api/v1/manage/models',{
        'household_daily_tokens':household,'member_daily_tokens':member})
    assert response.status_code==200,response.text


def debit(db,user_id,amount,*,key=None,status='SUCCEEDED',day=None,household='h1',reserved=None):
    row=ModelUsage(id=str(uuid.uuid4()),owner_id=user_id,household_id=household,
        task_id=str(uuid.uuid4()),call_key=key or str(uuid.uuid4()),
        day=day or datetime.now(timezone.utc).date().isoformat(),
        reserved=amount if reserved is None else reserved,charged=amount,status=status)
    db.add(row);db.flush();return row


def identity(alice):
    return Actor(alice.user_id,'h1',alice.device_id,'infrastructure_owner')


def test_budget_exception_preserves_message_and_structured_context():
    error=BudgetExceeded('需要缩小本次任务',code='task_budget_exceeded',scope='task',
        used=50,requested=20,limit=60,recoverable=False)
    assert error.status_code==409 and error.detail=='需要缩小本次任务'
    assert error.metadata=={'code':'task_budget_exceeded','scope':'task','used':50,
        'requested':20,'limit':60,'recoverable':False}
    assert error.code=='task_budget_exceeded' and error.scope=='task'
    assert BudgetExceeded('旧调用').metadata=={'code':'budget_exceeded','scope':'task',
        'used':None,'requested':None,'limit':None,'recoverable':True}


def test_household_limit_counts_all_members_and_only_current_day_charge(system,alice):
    bob=SignedClient(system[1],system[2]);limits(alice,household=100,member=90)
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        debit(db,alice.user_id,50,reserved=999)
        debit(db,bob.user_id,31,status='UNKNOWN')
        debit(db,alice.user_id,10000,day='2000-01-01')
        debit(db,alice.user_id,10000,household='other-household')
        db.commit()
        with pytest.raises(BudgetExceeded) as denied:
            reserve(system[0].state,db,identity(alice),SimpleNamespace(id='task'),'next-call',20)
        assert denied.value.metadata=={'code':'household_daily_budget_exceeded','scope':'household_daily',
            'used':81,'requested':20,'limit':100,'recoverable':True}
        assert '家庭' in denied.value.detail and '已占用 81' in denied.value.detail
        assert '下次需预留 20' in denied.value.detail and '上限 100' in denied.value.detail
        assert '不等于服务商实际用量' in denied.value.detail
        assert db.scalar(select(ModelUsage).where(ModelUsage.call_key=='next-call')) is None


def test_member_limit_is_distinct_from_available_household_limit(system,alice):
    bob=SignedClient(system[1],system[2]);limits(alice,household=500,member=50)
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        debit(db,alice.user_id,35,status='RESERVED');debit(db,bob.user_id,300);db.commit()
        with pytest.raises(BudgetExceeded) as denied:
            reserve(system[0].state,db,identity(alice),SimpleNamespace(id='task'),'member-blocked',16)
        assert denied.value.metadata=={'code':'member_daily_budget_exceeded','scope':'member_daily',
            'used':35,'requested':16,'limit':50,'recoverable':True}
        assert '本人' in denied.value.detail
        accepted=reserve(system[0].state,db,identity(alice),SimpleNamespace(id='task'),'member-at-limit',15)
        assert accepted.charged==accepted.reserved==15 and accepted.status=='RESERVED'


@pytest.mark.parametrize(('household','member','expected_scope'),[(0,50,'household_daily'),(50,0,'member_daily')])
def test_unconfigured_daily_limit_has_actionable_metadata(system,alice,household,member,expected_scope):
    limits(alice,household=household,member=member)
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        with pytest.raises(BudgetExceeded) as denied:
            reserve(system[0].state,db,identity(alice),SimpleNamespace(id='task'),'unconfigured',12)
        assert denied.value.metadata=={'code':'daily_budget_not_configured','scope':expected_scope,
            'used':0,'requested':12,'limit':0,'recoverable':True}
        assert '尚未配置' in denied.value.detail
        assert db.scalar(select(func.count()).select_from(ModelUsage))==0


@pytest.mark.parametrize('status',['RESERVED','UNKNOWN','SUCCEEDED'])
def test_duplicate_dispatch_is_not_a_recoverable_budget_error_even_if_limits_disabled(system,alice,status):
    limits(alice,household=0,member=0)
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        original=debit(db,alice.user_id,7,key='dispatched',status=status);db.commit()
        with pytest.raises(HTTPException) as denied:
            reserve(system[0].state,db,identity(alice),SimpleNamespace(id='task'),'dispatched',2)
        assert denied.value.status_code==409 and not isinstance(denied.value,BudgetExceeded)
        assert '调整额度不能解除' in denied.value.detail
        assert original.status==status and original.charged==7
        assert db.scalar(select(func.count()).select_from(ModelUsage))==1


def test_increasing_actual_daily_limit_allows_previously_unsent_call_without_duplicate_charge(system,alice):
    limits(alice,household=20,member=20)
    with system[2]() as db:
        scope(db,alice.user_id,'h1');debit(db,alice.user_id,10);db.commit()
        with pytest.raises(BudgetExceeded) as denied:
            reserve(system[0].state,db,identity(alice),SimpleNamespace(id='task'),'not-dispatched',30)
        assert denied.value.recoverable
        db.commit()
        assert db.scalar(select(func.count()).select_from(ModelUsage))==1
    limits(alice,household=100,member=100)
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        accepted=reserve(system[0].state,db,identity(alice),SimpleNamespace(id='task'),'not-dispatched',30)
        db.commit()
        assert accepted.charged==30
        assert db.scalar(select(func.sum(ModelUsage.charged)))==40
        assert db.scalar(select(func.count()).select_from(ModelUsage))==2
