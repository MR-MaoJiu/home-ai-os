"""真实服务集成测试：显式设置 HOMEAI_INTEGRATION=1 后运行。"""
import os
import uuid
import pytest
import nats
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from homeai.api import create_app
from homeai.config import Settings
from homeai.db import database, Record, scope
from homeai.runtime import run_task
from homeai.worker import cycle
from conftest import SignedClient
from test_security_data import put

pytestmark = pytest.mark.skipif(os.environ.get('HOMEAI_INTEGRATION') != '1', reason='需要项目独立 PostgreSQL/OPA/NATS')


@pytest.mark.asyncio
async def test_real_rls_opa_outbox():
    settings = Settings()
    settings.database_url = settings.database_url.rsplit("/", 1)[0] + "/homeai_test"
    app = create_app(settings)
    client = TestClient(app)
    household = str(uuid.uuid4())
    alice = SignedClient(client,app.state.db,household=household)
    bob = SignedClient(client,app.state.db,household=household)
    rid = put(alice)
    with app.state.db() as db:
        # 无 scope 时数据库自身拒绝看到私人行，独立于 API 层。
        assert db.get(Record,rid) is None
        assert db.scalar(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")) is False
        scope(db,bob.user_id,household)
        assert db.get(Record,rid) is None
    assert alice.request('PUT','/api/v1/data/'+rid+'/grants/'+bob.user_id).status_code==200
    assert bob.request('GET','/api/v1/data/'+rid).status_code==200
    assert alice.request('DELETE','/api/v1/data/'+rid+'/grants/'+bob.user_id).status_code==200
    assert bob.request('GET','/api/v1/data/'+rid).status_code==404
    response=alice.request('POST','/api/v1/tasks',{'idempotency_key':str(uuid.uuid4()),'capability':'reminder.create@v1','arguments':{'title':'集成测试提醒'}})
    assert response.status_code==202,response.text
    tid=response.json()['id']
    await run_task(app.state,tid,alice.user_id)
    assert alice.request('GET','/api/v1/tasks/'+tid).json()['status']=='SUCCEEDED'
    nc=await nats.connect(app.state.settings.nats_url)
    js=nc.jetstream()
    from nats.js.errors import NotFoundError
    try:await js.stream_info('HOMEAI')
    except NotFoundError:await js.add_stream(name='HOMEAI',subjects=['homeai.events.>'])
    await cycle(app.state,js)
    info=await js.stream_info('HOMEAI')
    assert info.state.messages > 0
    await nc.drain()


@pytest.mark.asyncio
async def test_pgvector_uses_canonical_content():
    import httpx
    from homeai.providers import Registry
    from homeai.contracts import ProviderManifest
    from homeai.db import Provider
    from homeai.vector_index import reconcile,search
    from homeai.security import Actor
    settings=Settings();settings.database_url=settings.database_url.rsplit('/',1)[0]+'/homeai_test'
    app=create_app(settings)
    app.state.registry=Registry(app.state.vault,httpx.MockTransport(lambda r:httpx.Response(200,json={'data':[{'embedding':[0.1,0.2,0.3]}]})))
    client=TestClient(app);household=str(uuid.uuid4());alice=SignedClient(client,app.state.db,household=household)
    with app.state.db() as db:
        manifest=ProviderManifest(id='test.embed',version='1',adapter='openai',endpoint='http://local.test/v1',model='test',capabilities={'model.embed@v1':'embed'},allowed_hosts=['local.test'])
        old=db.get(Provider,manifest.id)
        if old:old.manifest=manifest.model_dump_json();old.enabled=True
        else:db.add(Provider(id=manifest.id,manifest=manifest.model_dump_json(),enabled=True))
        db.commit()
    rid=put(alice,{'source':'manual','source_id':str(uuid.uuid4()),'kind':'memory.fact','version':1,'payload':{'content':'原始规范事实'}})
    await reconcile(app.state,alice.user_id,household)
    with app.state.db() as db:
        scope(db,alice.user_id,household)
        results=await search(app.state,db,Actor(alice.user_id,household,alice.device_id,'adult'),'相关问题')
        assert results[0]['id']==rid
        assert results[0]['payload']['content']=='原始规范事实'
    assert alice.request('DELETE','/api/v1/data/'+rid).status_code==200
    with app.state.db() as db:
        scope(db,alice.user_id,household)
        assert await search(app.state,db,Actor(alice.user_id,household,alice.device_id,'adult'),'相关问题')==[]


def test_real_continuous_sharing_rls():
    from homeai.db import SharingRule
    from test_security_data import test_continuous_sharing_is_scoped_and_reversible
    settings=Settings();settings.database_url=settings.database_url.rsplit('/',1)[0]+'/homeai_test'
    app=create_app(settings)
    with TestClient(app) as client:
        alice=SignedClient(client,app.state.db)
        try:
            test_continuous_sharing_is_scoped_and_reversible((app,client,app.state.db),alice)
            with app.state.db() as db:
                assert db.scalar(select(SharingRule).where(SharingRule.owner_id==alice.user_id)) is None
                scope(db,alice.user_id,'h1')
                assert db.scalar(select(SharingRule).where(SharingRule.owner_id==alice.user_id)) is not None
        finally:
            alice.request('DELETE','/api/v1/devices/'+alice.device_id)
    app.state.db.kw['bind'].dispose()


def test_real_agent_skill_household_policy():
    from test_integrations import test_instruction_skill_scope_and_disable
    settings=Settings();settings.database_url=settings.database_url.rsplit('/',1)[0]+'/homeai_test'
    app=create_app(settings)
    with TestClient(app) as client:
        alice=SignedClient(client,app.state.db,role='infrastructure_owner')
        try:test_instruction_skill_scope_and_disable((app,client,app.state.db),alice)
        finally:alice.request('DELETE','/api/v1/devices/'+alice.device_id)
    app.state.db.kw['bind'].dispose()
