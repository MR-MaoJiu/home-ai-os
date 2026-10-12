"""独立 PostgreSQL 测试库验证行锁、并发关闭和 RLS，不读取运行库。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from conftest import SignedClient
from homeai.api import create_app
from homeai.auto_review import process_pending
from homeai.config import Settings
from homeai.db import Approval, Audit, Task, scope
from test_auto_review import PATH, pending_approval, toggle

pytestmark = pytest.mark.skipif(os.environ.get('HOMEAI_INTEGRATION') != '1', reason='需要独立 homeai_test PostgreSQL 迁移到0033')


@pytest.fixture
def pg_system(tmp_path):
    settings = Settings()
    settings.database_url = settings.database_url.rsplit('/', 1)[0] + '/homeai_test'
    settings.state_dir = tmp_path
    settings.direct_enabled = False
    settings.encrypted_relay_enabled = False
    # 共享集成库使用同一测试主密钥，避免后台扫描读到其它密钥的夹具。
    app = create_app(settings)
    with TestClient(app) as client:
        yield app, client, app.state.db
    app.state.db.kw['bind'].dispose()


def test_concurrent_workers_approve_once_under_real_rls(pg_system):
    app, client, factory = pg_system
    alice = SignedClient(client, factory, role='infrastructure_owner')
    bob = SignedClient(client, factory)
    tid, aid = pending_approval(pg_system, alice)
    toggle(alice)
    with factory() as db:
        assert db.scalar(text('SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user')) is False
        assert db.get(Approval, aid) is None
        scope(db, bob.user_id, 'h1')
        assert db.get(Approval, aid) is None
    def run():
        return asyncio.run(process_pending(app.state, alice.user_id, 'h1', 'infrastructure_owner'))
    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(lambda _: run(), range(2)))
    assert sum(answers) == 1
    with factory() as db:
        scope(db, alice.user_id, 'h1')
        assert db.get(Task, tid).status == 'APPROVED'
        entries = list(db.scalars(select(Audit).where(Audit.resource_id == aid, Audit.action == 'approval.approved')))
        assert len(entries) == 1 and json.loads(entries[0].details)['automatic'] is True
    toggle(alice, False)
    alice.request('DELETE', '/api/v1/devices/' + alice.device_id)
    bob.request('DELETE', '/api/v1/devices/' + bob.device_id)


def test_disable_serializes_with_inflight_decision(pg_system):
    app, client, factory = pg_system
    alice = SignedClient(client, factory, role='infrastructure_owner')
    tid, aid = pending_approval(pg_system, alice)
    toggle(alice)
    entered, release = threading.Event(), threading.Event()
    original = app.state.policy
    class PausingPolicy:
        async def check(self, *args, **kwargs):
            entered.set()
            assert await asyncio.to_thread(release.wait, 10)
            await original.check(*args, **kwargs)
    app.state.policy = PausingPolicy()
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(lambda: asyncio.run(process_pending(app.state, alice.user_id, 'h1', 'infrastructure_owner')))
        assert entered.wait(10)
        closed = pool.submit(lambda: toggle(alice, False))
        try:
            assert not closed.done()
        finally:
            release.set()
        assert pending.result(timeout=10) == 1
        assert closed.result(timeout=10)['enabled'] is False
    next_tid, next_aid = pending_approval(pg_system, alice)
    assert asyncio.run(process_pending(app.state, alice.user_id, 'h1', 'infrastructure_owner')) == 0
    with factory() as db:
        scope(db, alice.user_id, 'h1')
        assert db.get(Approval, aid).decision == 'APPROVED'
        assert db.get(Approval, next_aid).decision == 'PENDING'
    alice.request('DELETE', '/api/v1/devices/' + alice.device_id)
