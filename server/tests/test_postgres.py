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
