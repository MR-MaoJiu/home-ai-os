"""真实 MLX 服务及 PostgreSQL/OPA 任务链路，无模型替身。"""
import os
import uuid
import httpx
import pytest
from fastapi.testclient import TestClient
from homeai.api import create_app
from homeai.config import Settings
from homeai.contracts import ProviderManifest
from homeai.db import Provider
from homeai.runtime import run_task
from conftest import SignedClient

pytestmark = pytest.mark.skipif(os.getenv('HOMEAI_MLX_TEST') != '1', reason='需要真实 MLX、PostgreSQL 和 OPA')
ENDPOINT = 'http://127.0.0.1:58086/v1'


def test_mlx_rejects_dynamic_models_and_invalid_limits():
    with httpx.Client(trust_env=False, timeout=20) as client:
        for extra in [dict(model='/tmp/unapproved-model'), dict(adapters='/tmp/adapter'),
                      dict(draft_model='/tmp/draft'), dict(max_tokens=-1),
                      dict(max_completion_tokens=0), dict(max_tokens=4097)]:
            response = client.post(ENDPOINT + '/chat/completions', json={
                'model': 'default_model', 'messages': [{'role': 'user', 'content': '你好'}], **extra})
            assert response.status_code == 400, response.text


@pytest.mark.asyncio
async def test_core_executes_real_mlx_generation():
    settings = Settings()
    settings.database_url = settings.database_url.rsplit('/', 1)[0] + '/homeai_test'
    app = create_app(settings)
    actor = SignedClient(TestClient(app), app.state.db, household=str(uuid.uuid4()), role='infrastructure_owner')
    manifest = ProviderManifest(id='a-mlx.' + uuid.uuid4().hex, version='0.31.3',
        adapter='openai', endpoint=ENDPOINT, model='default_model',
        capabilities={'model.generate@v1': 'chat'}, allowed_hosts=['127.0.0.1'])
    with app.state.db() as db:
        db.add(Provider(id=manifest.id, manifest=manifest.model_dump_json(), enabled=True))
        db.commit()
    try:
        with app.state.db() as db:
            assert app.state.registry.resolve(db, 'model.generate@v1').id == manifest.id
        response = actor.request('POST', '/api/v1/tasks', {
            'capability': 'model.generate@v1', 'message': '用一句中文介绍家庭助手。',
            'max_output_tokens': 96, 'idempotency_key': str(uuid.uuid4())})
        assert response.status_code == 202, response.text
        tid = response.json()['id']
        await run_task(app.state, tid, actor.user_id)
        task = actor.request('GET', '/api/v1/tasks/' + tid).json()
        assert task['status'] == 'SUCCEEDED', task
        assert task['result']['choices'][0]['message']['content'].strip()
        assert task['result']['usage']['completion_tokens'] > 0
    finally:
        with app.state.db() as db:
            db.get(Provider, manifest.id).enabled = False
            db.commit()
