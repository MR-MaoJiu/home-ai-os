"""实际 SearXNG、公开搜索查询与 Core 审批/披露闭环。"""
import json
import os
import uuid
from pathlib import Path
import pytest
from sqlalchemy import select
from homeai.db import Provider, Disclosure, Principal, scope
from homeai.runtime import run_task
from homeai.contracts import ProviderManifest
from test_workflows import workflow, create

pytestmark = pytest.mark.skipif(os.getenv('HOMEAI_INTEGRATION') != '1', reason='需要真实 PostgreSQL 与 OPA')


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv('HOMEAI_SEARXNG_TEST') != '1', reason='需要真实 SearXNG 与互联网搜索引擎')
async def test_public_search_runs_without_approval_returns_real_results_and_discloses(workflow):
    app, user = workflow
    manifest = ProviderManifest.model_validate_json(Path('providers/manifests/searxng.json').read_text())
    manifest.id = 'a.search.' + uuid.uuid4().hex
    with app.db() as db:
        db.add(Provider(id=manifest.id, manifest=manifest.model_dump_json(), enabled=True))
        db.commit()
    try:
        tid = create(user, [{'capability': 'web.search@v1', 'arguments': {'query': 'Python programming language'}}])
        await run_task(app, tid, user.user_id)
        assert user.request('GET', '/api/v1/approvals').json()==[]
        task = user.request('GET', '/api/v1/tasks/' + tid).json()
        assert task['status'] == 'SUCCEEDED', task
        assert task['result']['results'], task
        assert task['result']['content_trust'] == 'untrusted_web'
        assert any('python' in item['title'].lower() for item in task['result']['results'])
        with app.db() as db:
            principal=db.get(Principal,user.user_id)
            scope(db, user.user_id, principal.household_id)
            disclosure = db.scalar(select(Disclosure).where(Disclosure.task_id == tid))
            assert json.loads(disclosure.categories) == ['web_search_query']
            assert disclosure.bytes_sent > 0
            db.get(Provider, manifest.id).enabled = False
            db.commit()
        stopped = create(user, [{'capability': 'web.search@v1', 'arguments': {'query': 'Python documentation'}}])
        await run_task(app, stopped, user.user_id)
        assert user.request('GET','/api/v1/approvals').json()==[]
        assert user.request('GET', '/api/v1/tasks/' + stopped).json()['status'] == 'FAILED'
        assert user.request('GET', '/api/v1/tasks/' + tid).json()['result']['results']
    finally:
        with app.db() as db:
            db.get(Provider, manifest.id).enabled = False
            db.commit()


@pytest.mark.asyncio
async def test_sensitive_queries_fail_before_approval_and_network(workflow):
    app, user = workflow
    for query in ['联系 test@example.com', '电话 13800138000']:
        tid = create(user, [{'capability': 'web.search@v1', 'arguments': {'query': query}}])
        await run_task(app, tid, user.user_id)
        task = user.request('GET', '/api/v1/tasks/' + tid).json()
        assert task['status'] == 'FAILED'
        assert '个人信息' in task['error']
    assert user.request('GET', '/api/v1/approvals').json() == []
