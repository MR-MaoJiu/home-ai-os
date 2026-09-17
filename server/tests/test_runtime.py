import httpx
import pytest
from sqlalchemy import select
from homeai.runtime import run_task
from homeai.db import Task, Record, Approval
from homeai.policy import Policy


def submit(alice, **kwargs):
    response=alice.request('POST','/api/v1/tasks',{'idempotency_key':'request-0001',**kwargs})
    assert response.status_code==202,response.text
    return response.json()['id']


@pytest.mark.asyncio
async def test_real_internal_effect_and_idempotency(system,alice):
    tid=submit(alice,capability='reminder.create@v1',arguments={'title':'买牛奶'})
    assert submit(alice,capability='reminder.create@v1',arguments={'title':'买牛奶'})==tid
    await run_task(system[0].state,tid)
    await run_task(system[0].state,tid)
    result=alice.request('GET','/api/v1/tasks/'+tid).json()
    assert result['status']=='SUCCEEDED',result
    with system[2]() as db:
        assert len(list(db.scalars(select(Record))))==1
    assert alice.request('POST','/api/v1/tasks',{'idempotency_key':'request-0001','message':'different'}).status_code==409


@pytest.mark.asyncio
async def test_approval_bound_and_not_reusable(system,alice):
    tid=submit(alice,capability='mail.send@v1',arguments={'to':'example@example.com','subject':'测试','text':'测试'})
    await run_task(system[0].state,tid)
    assert alice.request('GET','/api/v1/tasks/'+tid).json()['status']=='AWAITING_APPROVAL'
    approval=alice.request('GET','/api/v1/approvals').json()[0]
    assert alice.request('POST','/api/v1/approvals/'+approval['id'],{'decision':'APPROVED'}).status_code==200
    assert alice.request('POST','/api/v1/approvals/'+approval['id'],{'decision':'APPROVED'}).status_code==409
    await run_task(system[0].state,tid)
    # 未配置邮件服务绝不能报告已发送。
    assert alice.request('GET','/api/v1/tasks/'+tid).json()['status']!='SUCCEEDED'


@pytest.mark.asyncio
async def test_policy_fail_closed(system,alice):
    system[0].state.policy=Policy('http://opa',httpx.MockTransport(lambda r:httpx.Response(503)))
    tid=submit(alice,capability='reminder.create@v1',arguments={'title':'不能创建'})
    await run_task(system[0].state,tid)
    assert alice.request('GET','/api/v1/tasks/'+tid).json()['status']=='FAILED'
    with system[2]() as db:
        assert list(db.scalars(select(Record)))==[]


@pytest.mark.asyncio
async def test_cloud_private_free_text_denied(system,alice):
    tid=submit(alice,message='我家住某个地址',mode='cloud')
    await run_task(system[0].state,tid)
    result=alice.request('GET','/api/v1/tasks/'+tid).json()
    assert result['status']=='FAILED'
    assert '云端' in result['error']


def test_unknown_capability_and_secret_denied(alice):
    assert alice.request('POST','/api/v1/tasks',{'idempotency_key':'unknown-0001','capability':'shell.exec'}).status_code==422
    assert alice.request('POST','/api/v1/tasks',{'idempotency_key':'secret-00001','message':'password: abcdefghijklmnop'}).status_code==422


def test_refresh_rotation_and_revocation(system,alice):
    from homeai.security import credential
    with system[2]() as db:
        refresh=credential(db,alice.user_id,'refresh',1000,alice.device_id)
        db.commit()
    access=alice.token
    alice.token=refresh
    response=alice.request('POST','/api/v1/session/renew')
    assert response.status_code==200,response.text
    assert response.json()['refresh_token']!=refresh
    assert alice.request('POST','/api/v1/session/renew').status_code==401
    alice.token=access
    assert alice.request('GET','/api/v1/me').status_code==200


def test_validation_does_not_echo_secrets(alice):
    result=alice.request('POST','/api/v1/secrets',{'provider_id':'x','value':'do-not-echo-this-secret','unexpected':'field'})
    assert result.status_code==422
    assert 'do-not-echo-this-secret' not in result.text


def test_untrusted_task_cannot_write_derived_memory(alice):
    result=alice.request('POST','/api/v1/tasks',{'idempotency_key':'derived-write-0001','capability':'memory.semantic.index@v1','arguments':{'record_id':'fake','content':'伪造事实'}})
    assert result.status_code==403


def test_access_token_cannot_mint_refresh_credentials(alice):
    assert alice.request('POST','/api/v1/session/renew').status_code==401
