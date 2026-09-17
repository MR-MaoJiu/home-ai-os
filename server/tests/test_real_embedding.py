"""使用真实 llama.cpp Embedding、OPA 和 PostgreSQL，不替换 HTTP 返回值。"""
import os
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from homeai.api import create_app
from homeai.config import Settings
from homeai.contracts import ProviderManifest
from homeai.db import Provider, MemoryVector, scope
from homeai.vector_index import reconcile
from conftest import SignedClient
from test_security_data import put

pytestmark = pytest.mark.skipif(os.environ.get('HOMEAI_EMBEDDING_TEST') != '1', reason='需要真实 Embedding 模型、OPA 和独立测试库')


@pytest.mark.asyncio
async def test_embedding_rebuild_update_isolation_delete():
    settings = Settings()
    settings.database_url = settings.database_url.rsplit('/', 1)[0] + '/homeai_test'
    app = create_app(settings)
    client = TestClient(app)
    household = str(uuid.uuid4())
    alice = SignedClient(client, app.state.db, household=household)
    bob = SignedClient(client, app.state.db, household=household)
    manifest = ProviderManifest(id='a.real.embedding', version='0f741b5a6585bd53aeb15cd1372c56f2a0f65e12', adapter='openai', endpoint='http://127.0.0.1:58081/v1', model='embeddinggemma-300M-Q8_0.gguf', allowed_hosts=['127.0.0.1'], capabilities={'model.embed@v1': 'embed'}, embedding_query_prefix='task: search result | query: ', embedding_document_prefix='title: none | text: ')
    with app.state.db() as db:
        old = db.get(Provider, manifest.id)
        if old:
            old.manifest, old.enabled = manifest.model_dump_json(), True
        else:
            db.add(Provider(id=manifest.id, manifest=manifest.model_dump_json(), enabled=True))
        db.commit()
    try:
        source_id = str(uuid.uuid4())
        body = {'source': 'integration', 'source_id': source_id, 'kind': 'memory.fact', 'version': 1, 'payload': {'content': '我喜欢周末去山里徒步。'}}
        rid = put(alice, body)
        await reconcile(app.state, alice.user_id, household)
        state = alice.request('GET', '/api/v1/memory/index').json()
        assert state['ready'] == 1, state
        result = alice.request('GET', '/api/v1/memory/search?q=周末运动').json()
        assert result['mode'] == 'pgvector_exact', result
        assert result['records'][0]['id'] == rid
        from homeai.runtime import run_task
        task = alice.request('POST', '/api/v1/tasks', {'idempotency_key': str(uuid.uuid4()), 'capability': 'memory.search@v1', 'arguments': {'query': '周末运动'}})
        assert task.status_code == 202, task.text
        await run_task(app.state, task.json()['id'], alice.user_id)
        completed = alice.request('GET', '/api/v1/tasks/' + task.json()['id']).json()
        assert completed['status'] == 'SUCCEEDED', completed
        assert completed['result']['mode'] == 'pgvector_exact'
        assert completed['result']['records'][0]['id'] == rid
        assert bob.request('GET', '/api/v1/memory/index').json()['eligible'] == 0
        assert bob.request('GET', '/api/v1/memory/search?q=周末运动').json()['records'] == []
        with app.state.db() as db:
            scope(db, alice.user_id, household)
            dimensions = db.scalar(text('SELECT vector_dims(search_vector) FROM memory_vectors WHERE record_id=:id'), {'id': rid})
            assert dimensions == 768
        body['version'] = 2
        body['payload'] = {'content': '周末改为在家看电影。'}
        put(alice, body)
        assert alice.request('GET', '/api/v1/memory/index').json()['pending'] == 1
        assert alice.request('GET', '/api/v1/memory/search?q=徒步').json()['records'] == []
        await reconcile(app.state, alice.user_id, household)
        assert alice.request('GET', '/api/v1/memory/index').json()['ready'] == 1
        assert alice.request('POST', '/api/v1/memory/index/rebuild').status_code == 200
        assert alice.request('GET', '/api/v1/memory/index').json()['pending'] == 1
        await reconcile(app.state, alice.user_id, household)
        # 同一 Provider 改版本也必须重新建立索引。
        manifest.version += '.rebuild'
        with app.state.db() as db:
            db.get(Provider, manifest.id).manifest = manifest.model_dump_json()
            db.commit()
        assert alice.request('GET', '/api/v1/memory/index').json()['pending'] == 1
        await reconcile(app.state, alice.user_id, household)
        assert alice.request('GET', '/api/v1/memory/index').json()['ready'] == 1
        assert alice.request('DELETE', '/api/v1/data/' + rid).status_code == 200
        await reconcile(app.state, alice.user_id, household)
        assert alice.request('GET', '/api/v1/memory/search?q=电影').json()['records'] == []
        with app.state.db() as db:
            scope(db, alice.user_id, household)
            assert db.scalar(select(MemoryVector).where(MemoryVector.record_id == rid)) is None
    finally:
        with app.state.db() as db:
            db.get(Provider, manifest.id).enabled = False
            db.commit()
