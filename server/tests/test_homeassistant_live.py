"""实际 Home Assistant 软件辅助实体的读取、授权与审批控制。"""
import json
import os
import uuid
from pathlib import Path
import httpx
import pytest
from homeai.db import Principal, Provider, Secret, uid, scope
from homeai.contracts import ProviderManifest
from homeai.runtime import run_task
from test_workflows import workflow, create

pytestmark = pytest.mark.skipif(os.getenv('HOMEAI_INTEGRATION') != '1' or os.getenv('HOMEAI_HOMEASSISTANT_TEST') != '1', reason='需要独立真实 Home Assistant 协议验收实例')


@pytest.mark.asyncio
async def test_real_entity_state_control_and_bound_approval(workflow):
    app, user = workflow
    base = 'http://127.0.0.1:58123'
    saved = json.loads(Path('state/homeassistant-test-auth.json').read_text())
    entity = 'switch.home_ai_protocol_switch'
    provider_id = 'a.homeassistant.' + uuid.uuid4().hex
    with httpx.Client(timeout=10, trust_env=False) as client:
        response = client.post(base + '/auth/token', data={'grant_type': 'refresh_token', 'refresh_token': saved['refresh_token'], 'client_id': base + '/'})
        response.raise_for_status(); token = response.json()['access_token']
    with app.db() as db:
        principal = db.get(Principal, user.user_id); scope(db, user.user_id, principal.household_id)
        secret_id = uid()
        db.add(Secret(id=secret_id, owner_id=user.user_id, household_id=principal.household_id, provider_id=provider_id,
            value=app.vault.seal(token, user.user_id + ':secret:' + secret_id)))
        manifest = ProviderManifest(id=provider_id, version='2026.9.2', adapter='homeassistant', endpoint=base,
            allowed_hosts=['127.0.0.1'], secret_id=secret_id, home_entities=[entity],
            capabilities={'home.states@v1': 'states', 'home.execute@v1': 'execute'})
        db.add(Provider(id=provider_id, manifest=manifest.model_dump_json(), enabled=True)); db.commit()
    with httpx.Client(timeout=10, trust_env=False, headers={'Authorization': 'Bearer ' + token}) as client:
        # 只操作本机软件辅助开关，不连接物理设备。
        client.post(base + '/api/services/switch/turn_off', json={'entity_id': entity}).raise_for_status()
        try:
            tid = create(user, [{'capability': 'home.states@v1'}])
            await run_task(app, tid, user.user_id)
            states = user.request('GET', '/api/v1/tasks/' + tid).json()
            assert states['status'] == 'SUCCEEDED', states
            assert [item['entity_id'] for item in states['result']] == [entity]
            assert states['result'][0]['state'] == 'off'
            denied = create(user, [{'capability': 'home.states@v1', 'arguments': {'entity_ids': ['input_boolean.homeai_protocol']}}])
            await run_task(app, denied, user.user_id)
            assert user.request('GET', '/api/v1/tasks/' + denied).json()['status'] == 'FAILED'
            operation = {'domain': 'switch', 'service': 'turn_on', 'entity_id': entity}
            control = create(user, [{'capability': 'home.execute@v1', 'arguments': operation}])
            await run_task(app, control, user.user_id)
            assert user.request('GET', '/api/v1/tasks/' + control).json()['status'] == 'AWAITING_APPROVAL'
            assert client.get(base + '/api/states/' + entity).json()['state'] == 'off'
            pending = user.request('GET', '/api/v1/approvals').json()[0]
            assert user.request('POST', '/api/v1/approvals/' + pending['id'], {'decision': 'APPROVED'}).status_code == 200
            await run_task(app, control, user.user_id)
            result = user.request('GET', '/api/v1/tasks/' + control).json()
            assert result['status'] == 'SUCCEEDED', result
            assert result['result']['status'] == 'service_completed'
            assert client.get(base + '/api/states/' + entity).json()['state'] == 'on'
            stale = create(user, [{'capability': 'home.execute@v1', 'arguments': {**operation, 'service': 'turn_off'}}])
            await run_task(app, stale, user.user_id)
            pending = user.request('GET', '/api/v1/approvals').json()[0]
            assert user.request('POST', '/api/v1/approvals/' + pending['id'], {'decision': 'APPROVED'}).status_code == 200
            with app.db() as db:
                manifest.home_entities.append('input_boolean.homeai_protocol')
                db.get(Provider, provider_id).manifest = manifest.model_dump_json(); db.commit()
            await run_task(app, stale, user.user_id)
            assert user.request('GET', '/api/v1/tasks/' + stale).json()['status'] == 'FAILED'
            assert client.get(base + '/api/states/' + entity).json()['state'] == 'on'
        finally:
            client.post(base + '/api/services/switch/turn_off', json={'entity_id': entity}).raise_for_status()
            with app.db() as db:
                db.get(Provider, provider_id).enabled = False; db.commit()
