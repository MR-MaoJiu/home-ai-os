"""真实依赖的只读诊断，不用固定成功值或替代服务。"""
import os
import uuid
import nats
import pytest
from homeai.diagnostics import inspect
from homeai.security import Actor
from homeai.db import Principal
from test_workflows import workflow
from conftest import SignedClient

pytestmark = pytest.mark.skipif(os.getenv('HOMEAI_INTEGRATION') != '1', reason='需要 PostgreSQL、OPA 和 NATS')


@pytest.mark.asyncio
async def test_dependencies_and_admin_boundary(workflow):
    app, user = workflow
    assert user.request('GET', '/api/v1/manage/readiness').status_code == 403
    admin = SignedClient(user.client, app.db, role='infrastructure_owner')
    name = 'DIAGNOSTIC_' + uuid.uuid4().hex
    prefix = 'diagnostic.' + uuid.uuid4().hex
    app.settings.event_stream, app.settings.event_subject_prefix = name, prefix
    nc = await nats.connect(app.settings.nats_url)
    try:
        await nc.jetstream().add_stream(name=name, subjects=[prefix + '.>'])
        response = admin.request('GET', '/api/v1/manage/readiness')
        assert response.status_code == 200
        result = response.json()
        assert result['core_dependencies_ready'], result
        assert result['checks']['database']['restricted_role']
        assert result['checks']['database']['forced_rls']
        assert result['production_sandbox_verified'] is False
        assert 'postgresql://' not in response.text and 'password' not in response.text
    finally:
        await nc.jetstream().delete_stream(name)
        await nc.close()


@pytest.mark.asyncio
async def test_missing_stream_is_not_reported_as_ready(workflow):
    app, user = workflow
    app.settings.event_stream = 'MISSING_' + uuid.uuid4().hex
    with app.db() as db:
        principal = db.get(Principal, user.user_id)
        actor = Actor(user.user_id, principal.household_id, user.device_id, principal.role)
    result = await inspect(app, actor)
    assert result['database'] and result['master_key_loaded']
    assert not result['core_dependencies_ready']
    assert not result['checks']['events']['ok']
