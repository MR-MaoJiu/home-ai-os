"""真实 Home Assistant 订阅、持久观察、重连快照与授权撤回。"""
import asyncio
import json
import os
import uuid
from pathlib import Path
import httpx
import pytest
from sqlalchemy import select
from homeai.contracts import ProviderManifest
from homeai.db import Principal, Provider, Secret, HomeObservation, Outbox, scope, uid
from homeai.security import Actor
from homeai.home_observer import watch, lease, binding
from test_workflows import workflow
from conftest import SignedClient

pytestmark = pytest.mark.skipif(os.getenv('HOMEAI_INTEGRATION') != '1' or os.getenv('HOMEAI_HOMEASSISTANT_TEST') != '1', reason='需要真实 Home Assistant 与 PostgreSQL/OPA')


@pytest.mark.asyncio
async def test_observer_persists_changes_resyncs_and_revokes(workflow):
    app, user = workflow
    base = 'http://127.0.0.1:58123'
    credentials = json.loads(Path('state/homeassistant-test-auth.json').read_text())
    entity = 'switch.home_ai_protocol_switch'
    provider_id = 'observer.' + uuid.uuid4().hex
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.post(base + '/auth/token', data={'grant_type': 'refresh_token', 'refresh_token': credentials['refresh_token'], 'client_id': base + '/'})
        response.raise_for_status(); token = response.json()['access_token']
        client.headers['Authorization'] = 'Bearer ' + token
        (await client.post(base + '/api/services/switch/turn_off', json={'entity_id': entity})).raise_for_status()
        with app.db() as db:
            principal = db.get(Principal, user.user_id)
            actor = Actor(user.user_id, principal.household_id, user.device_id, principal.role)
            scope(db, actor.user_id, actor.household_id)
            secret_id = uid()
            db.add(Secret(id=secret_id, owner_id=actor.user_id, household_id=actor.household_id, provider_id=provider_id,
                value=app.vault.seal(token, actor.user_id + ':secret:' + secret_id)))
            manifest = ProviderManifest(id=provider_id, version='2026.9.2', adapter='homeassistant', endpoint=base,
                allowed_hosts=['127.0.0.1'], secret_id=secret_id, home_entities=[entity], home_events=True, capabilities={'home.states@v1': 'states'})
            db.add(Provider(id=provider_id, manifest=manifest.model_dump_json(), enabled=True)); db.commit()
        operation = asyncio.create_task(watch(app, actor, provider_id, 'observer-one'))
        async def wait_state(expected):
            for _ in range(300):
                response = user.request('GET', '/api/v1/home/observations')
                assert response.status_code == 200, response.text
                rows = response.json()
                if rows and rows[0]['status'] == 'CONNECTED' and rows[0]['states'] and rows[0]['states'][0]['state'].get('state') == expected:
                    return rows[0]
                if operation.done():
                    await operation
                    raise AssertionError('观察者提前结束')
                await asyncio.sleep(0.1)
            raise AssertionError('真实状态没有同步')
        try:
            first = await wait_state('off')
            assert len(first['states']) == 1
            with app.db() as db:
                _, _, signature = binding(db, actor, provider_id)
            assert lease(app, actor, provider_id, 'observer-two', signature) is False
            (await client.post(base + '/api/services/switch/turn_on', json={'entity_id': entity})).raise_for_status()
            changed = await wait_state('on')
            assert changed['states'][0]['revision'] > first['states'][0]['revision']
            # 实际停止远端服务，验证同一观察者自动重连，不以手工重建任务代替。
            import subprocess
            command = ['docker', 'compose', '-f', 'deploy/compose.homeassistant-test.yml']
            await asyncio.to_thread(subprocess.run, command + ['stop'], check=True, capture_output=True)
            try:
                for _ in range(100):
                    if user.request('GET', '/api/v1/home/observations').json()[0]['status'] == 'DISCONNECTED': break
                    await asyncio.sleep(0.1)
                else: raise AssertionError('远端断开后没有标记离线')
            finally:
                await asyncio.to_thread(subprocess.run, command + ['start'], check=True, capture_output=True)
            await wait_state('off')
            (await client.post(base + '/api/services/switch/turn_on', json={'entity_id': entity})).raise_for_status()
            changed = await wait_state('on')
            other = SignedClient(user.client, app.db, household=actor.household_id)
            assert other.request('GET', '/api/v1/home/observations').json() == []
            operation.cancel(); await asyncio.gather(operation, return_exceptions=True)
            assert user.request('GET', '/api/v1/home/observations').json()[0]['status'] == 'DISCONNECTED'
            (await client.post(base + '/api/services/switch/turn_off', json={'entity_id': entity})).raise_for_status()
            operation = asyncio.create_task(watch(app, actor, provider_id, 'observer-two'))
            restored = await wait_state('off')
            assert restored['states'][0]['revision'] > changed['states'][0]['revision']
            with app.db() as db:
                scope(db, actor.user_id, actor.household_id)
                row = db.scalar(select(HomeObservation).where(HomeObservation.provider_id == provider_id))
                assert 'entity_id' not in row.payload
                assert db.scalar(select(Outbox).where(Outbox.resource_id == row.id, Outbox.kind == 'home.state_changed'))
                db.get(Provider, provider_id).enabled = False; db.commit()
            await asyncio.wait_for(operation, timeout=7)
            assert user.request('GET', '/api/v1/home/observations').json() == []
        finally:
            operation.cancel(); await asyncio.gather(operation, return_exceptions=True)
            await client.post(base + '/api/services/switch/turn_off', json={'entity_id': entity})
            with app.db() as db:
                db.get(Provider, provider_id).enabled = False; db.commit()
