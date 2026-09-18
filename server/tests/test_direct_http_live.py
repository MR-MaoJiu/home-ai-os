"""真实 PostgreSQL 与 UDP/DTLS 通道上的 Core 业务验收，不替换业务 API。"""
import asyncio
import json
import os
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric import ec

from homeai.api import create_app
from homeai.config import Settings
from homeai.crypto import Vault
from homeai.direct_http import DirectHTTPClient, validate_request
from homeai.direct_sessions import sessions
from homeai.server_identity import identity
from conftest import SignedClient

pytestmark = pytest.mark.skipif(os.environ.get('HOMEAI_DIRECT_INTEGRATION') != '1', reason='需要 aiortc 和真实 PostgreSQL')


@pytest.mark.asyncio
async def test_direct_core_auth_sync_and_revocation(tmp_path):
    from homeai.direct_transport import DirectPeer
    settings = Settings(state_dir=tmp_path, direct_enabled=True)
    settings.database_url = settings.database_url.rsplit('/', 1)[0] + '/homeai_test'
    app = create_app(settings, vault=Vault(os.urandom(32)))
    alice = SignedClient(TestClient(app), app.state.db, household=str(uuid.uuid4()))
    bob = SignedClient(TestClient(app), app.state.db, household=str(uuid.uuid4()))
    _, server_key = identity(app.state)
    peer = DirectPeer(alice.key, server_key.public_key())
    manager = sessions(app)
    try:
        envelope = await peer.offer()
        raw = json.dumps({'envelope': envelope}).encode()
        path = '/api/v1/direct/offer'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://homeai.direct') as client:
            answer = await client.post(path, content=raw, headers=alice.headers('POST', path, raw))
            assert answer.status_code == 200, answer.text
            replay = await client.post(path, content=raw, headers=alice.headers('POST', path, raw))
            assert replay.status_code == 409
        await peer.accept(answer.json())
        bridge = DirectHTTPClient(peer)
        async def request(actor, method, target, body=b''):
            return await bridge.request(method, target, body=body, headers=actor.headers(method, target, body))
        meta, data = await request(alice, 'GET', '/api/v1/me')
        assert meta['status'] == 200 and json.loads(data)['device_id'] == alice.device_id
        meta, _ = await request(bob, 'GET', '/api/v1/me')
        assert meta['status'] == 401  # 有效凭据也不能冒用另一设备的直连通道。
        headers = alice.headers('GET', '/api/v1/me', b'')
        assert (await bridge.request('GET', '/api/v1/me', headers=headers))[0]['status'] == 200
        assert (await bridge.request('GET', '/api/v1/me', headers=headers))[0]['status'] == 401
        payload = '直连文件内容-' + os.urandom(512 * 1024).hex()
        body = json.dumps({'batch_id': str(uuid.uuid4()), 'records': [{'source': 'manual', 'source_id': str(uuid.uuid4()),
                           'kind': 'document.text', 'version': 1, 'payload': {'content': payload}}]}).encode()
        meta, data = await request(alice, 'POST', '/api/v1/data/sync', body)
        assert meta['status'] == 200, data[:200]
        meta, data = await request(alice, 'GET', '/api/v1/data')
        assert meta['status'] == 200 and payload in data.decode()
        # 设备撤销使用真实 Core 路由，已建立的通道下一请求必须拒绝。
        meta, _ = await request(alice, 'DELETE', '/api/v1/devices/' + alice.device_id)
        assert meta['status'] == 200
        meta, _ = await request(alice, 'GET', '/api/v1/data')
        assert meta['status'] == 401
    finally:
        await manager.close()
        await peer.close()
        bob.request('DELETE', '/api/v1/devices/' + bob.device_id)
