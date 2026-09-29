"""只在独立 homeai_test 验证真实 PostgreSQL RLS、并发响应和任务恢复；不投递 APNs。"""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from homeai.api import create_app
from homeai.config import Settings
from homeai.db import Task,Principal,Invocation,Approval,scope,now
from homeai.client_actions import ClientAction,TaskDataGrant,create_request,execute,authorized_source_scope
from homeai.security import Actor
from homeai.crypto import canonical,digest
from conftest import SignedClient
from test_security_data import put,record

pytestmark=pytest.mark.skipif(os.environ.get('HOMEAI_INTEGRATION')!='1',reason='需要已迁移的独立PostgreSQL测试库')


def test_real_rls_concurrent_response_and_revoke(tmp_path):
    settings=Settings();settings.database_url=settings.database_url.rsplit('/',1)[0]+'/homeai_test';settings.state_dir=tmp_path
    app=create_app(settings);client=TestClient(app)
    household=str(uuid.uuid4())
    alice=SignedClient(client,app.state.db,household=household)
    bob=SignedClient(client,app.state.db,household=household)
    other=SignedClient(client,app.state.db,household=household)
    try:
        tid=alice.request('POST','/api/v1/tasks',{'idempotency_key':str(uuid.uuid4()),'message':'隔离测试请求'}).json()['id']
        actor=Actor(alice.user_id,household,alice.device_id,'adult')
        with app.state.db() as db:
            scope(db,alice.user_id,household);task=db.get(Task,tid)
            body=app.state.vault.open(task.request,alice.user_id+':task:'+tid)
            body.update(_task_id=tid,_mentions=[{'member_id':bob.user_id}]);task.request=app.state.vault.seal(body,alice.user_id+':task:'+tid)
            result=create_request(app.state,db,actor,task,'data.share','逐次授权测试',{},target_member_id=bob.user_id)
            aid=result['client_action_id'];db.commit()
        with app.state.db() as db:
            scope(db,other.user_id,household)
            assert db.get(ClientAction,aid) is None
        rid=put(bob,record(source_id=str(uuid.uuid4()),payload={'title':'隔离样本'}))
        body={'idempotency_key':str(uuid.uuid4()),'record_ids':[rid]}
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses=list(executor.map(lambda _:bob.request('POST','/api/v1/client-actions/'+aid+'/respond',body),range(2)))
        assert [r.status_code for r in responses]==[200,200],[r.text for r in responses]
        assert responses[0].json()==responses[1].json()
        assert alice.request('GET','/api/v1/tasks/'+tid).json()['status']=='RECEIVED'
        assert alice.request('GET','/api/v1/data/'+rid).status_code==404
        assert alice.request('GET','/api/v1/tasks/'+tid+'/data/'+rid).status_code==200
        with app.state.db() as db:
            scope(db,bob.user_id,household)
            assert len(list(db.scalars(select(TaskDataGrant).where(TaskDataGrant.action_id==aid))))==1
            scope(db,other.user_id,household)
            assert list(db.scalars(select(TaskDataGrant).where(TaskDataGrant.action_id==aid)))==[]
        with app.state.db() as db:
            scope(db,alice.user_id,household);task=db.get(Task,tid)
            payload=app.state.vault.open(task.request,alice.user_id+':task:'+tid)
            with authorized_source_scope(db,actor,rid,payload) as source:
                assert source.user_id==bob.user_id and db.info['user_id']==bob.user_id
            assert db.info['user_id']==alice.user_id
            arguments={'target_member_id':bob.user_id,'summary':'仅归还原资料所有者的测试摘要'}
            invocation=Invocation(id=str(uuid.uuid4()),owner_id=alice.user_id,household_id=household,task_id=tid,step=0,
                capability='member.notify@v1',arguments='encrypted-test-fixture',arguments_hash=digest(canonical(arguments)))
            db.add(invocation);db.flush()
            db.add(Approval(owner_id=alice.user_id,household_id=household,invocation_id=invocation.id,
                arguments_hash=invocation.arguments_hash,expires_at=now()+300,decision='APPROVED'));db.flush()
            message=execute(app.state,db,actor,task,invocation,'member.notify@v1',arguments)['client_action_id'];db.commit()
        assert bob.request('POST','/api/v1/client-actions/'+aid+'/revoke').status_code==200
        hidden=bob.request('GET','/api/v1/client-actions/'+message).json()
        assert hidden['status']=='REVOKED' and '仅归还原资料' not in str(hidden)
        assert alice.request('GET','/api/v1/tasks/'+tid+'/data/'+rid).status_code==403
        assert alice.request('POST','/api/v1/tasks/'+tid+'/cancel').status_code==200
    finally:
        client.close();app.state.db.kw['bind'].dispose()
