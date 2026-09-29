"""通知配置只进行结构检查；协议模拟不等于 Apple 实际投递成功。"""
import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from conftest import SignedClient
from homeai.notifications import APNs, APNsConfigurationUnavailable
from homeai.notifications_config import configuration_path, resolve_configuration
from test_notifications import configured

PATH = '/api/v1/manage/notifications'


def pem():
    return ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


def body(private_key=None, **changes):
    result = {'enabled': True, 'key_id':'AAAAAAAAAA', 'team_id':'BBBBBBBBBB', 'topic':'org.homeai.test'}
    if private_key is not None:
        result['private_key'] = private_key
    return {**result, **changes}


def test_configuration_requires_admin_and_key_is_never_returned(system, alice):
    bob = SignedClient(system[1], system[2])
    assert bob.request('GET', PATH).status_code == 403
    assert bob.request('PUT', PATH, body(pem())).status_code == 403
    first = alice.request('GET', PATH)
    assert first.status_code == 200
    assert first.json()['source'] == 'none' and first.json()['status'] == 'not_configured'
    secret = pem()
    response = alice.request('PUT', PATH, body(secret))
    assert response.status_code == 200, response.text
    assert response.json()['validation'] == 'structure_only'
    assert response.json()['private_key_configured'] and response.json()['push_configured']
    assert response.json()['source'] == 'managed'
    assert 'private_key' not in response.json() and secret not in response.text
    path = configuration_path(system[0].state)
    assert path.stat().st_mode & 0o777 == 0o600
    assert 'PRIVATE KEY' not in path.read_text() and secret not in path.read_text()
    assert resolve_configuration(system[0].state).private_key == secret.strip()
    assert alice.request('GET', PATH).json() == response.json()
    assert bob.request('GET', '/api/v1/notifications/status').json()['push_configured']


def test_configuration_validates_private_key_and_requires_complete_enable(system, alice):
    assert alice.request('PUT', PATH, body()).status_code == 422
    invalid = 'PRIVATE INVALID NEVER LOG THIS'
    response = alice.request('PUT', PATH, body(invalid))
    assert response.status_code == 422 and invalid not in response.text
    assert alice.request('PUT', PATH, body(pem(), key_id='INVALID')).status_code == 422
    assert not configuration_path(system[0].state).exists()
    response = alice.request('PUT', PATH, {'enabled':False})
    assert response.status_code == 200 and response.json()['status'] == 'disabled'
    assert response.json()['source'] == 'managed' and not response.json()['private_key_configured']


def test_environment_fallback_and_explicit_disable_preserve_key(system, alice, tmp_path):
    configured(system[0].state.settings, tmp_path)
    first = alice.request('GET', PATH).json()
    assert first['source'] == 'environment' and first['private_key_configured']
    old_key = resolve_configuration(system[0].state).private_key
    disabled = alice.request('PUT', PATH, body('', enabled=False))
    assert disabled.status_code == 200 and disabled.json()['status'] == 'disabled'
    assert disabled.json()['source'] == 'managed' and not disabled.json()['push_configured']
    assert resolve_configuration(system[0].state).private_key == old_key
    assert alice.request('GET', '/api/v1/notifications/status').json()['status'] == 'disabled'
    enabled = alice.request('PUT', PATH, body())
    assert enabled.status_code == 200 and enabled.json()['push_configured']
    assert resolve_configuration(system[0].state).private_key == old_key
    path = configuration_path(system[0].state)
    path.write_text('invalid encrypted configuration')
    broken = alice.request('GET', PATH).json()
    assert broken['status'] == 'invalid_configuration' and broken['source'] == 'managed'
    assert not broken['push_configured']


@pytest.mark.asyncio
async def test_worker_reads_saved_configuration_rotations_and_disable_without_restart(system, alice):
    captured = []
    def receive(request):
        captured.append(request)
        return httpx.Response(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        worker = APNs(system[0].state.settings, client, app=system[0].state)
        assert await worker.refresh() == 'not_configured'
        assert alice.request('PUT', PATH, body(pem())).status_code == 200
        await worker.send('ab'*32, 'sandbox', '11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222')
        first_auth = captured[-1].headers['authorization']
        assert captured[-1].headers['apns-topic'] == 'org.homeai.test'
        assert alice.request('PUT', PATH, body(pem(), key_id='CCCCCCCCCC', topic='org.homeai.changed')).status_code == 200
        await worker.send('ab'*32, 'sandbox', '11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222')
        header = captured[-1].headers['authorization'].split(' ')[1].split('.')[0]
        assert json.loads(base64.urlsafe_b64decode(header+'=='))['kid'] == 'CCCCCCCCCC'
        assert captured[-1].headers['authorization'] != first_auth
        assert captured[-1].headers['apns-topic'] == 'org.homeai.changed'
        assert alice.request('PUT', PATH, body('', enabled=False, key_id='CCCCCCCCCC', topic='org.homeai.changed')).status_code == 200
        with pytest.raises(APNsConfigurationUnavailable):
            await worker.send('ab'*32, 'sandbox', '11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222')
        assert len(captured) == 2


@pytest.mark.asyncio
async def test_worker_rebuilds_its_http2_pool_when_configuration_changes(system, alice):
    assert alice.request('PUT', PATH, body(pem())).status_code == 200
    worker = APNs(system[0].state.settings, app=system[0].state)
    try:
        assert await worker.refresh() == 'ready'
        original = worker.client
        assert original and not original.is_closed
        assert alice.request('PUT', PATH, body('', topic='org.homeai.changed')).status_code == 200
        assert await worker.refresh() == 'ready'
        assert original.is_closed and worker.client is not original
        updated = worker.client
        assert alice.request('PUT', PATH, body('', enabled=False, topic='org.homeai.changed')).status_code == 200
        assert await worker.refresh() == 'disabled'
        assert updated.is_closed and worker.client is None
    finally:
        await worker.close()


def test_configuration_file_permissions_fail_closed_without_environment_fallback(system, alice, tmp_path):
    configured(system[0].state.settings, tmp_path)
    assert alice.request('PUT', PATH, body('', enabled=False)).status_code == 200
    path = configuration_path(system[0].state)
    path.chmod(0o644)
    response = alice.request('GET', PATH)
    assert response.status_code == 200
    assert response.json()['status'] == 'invalid_configuration'
    assert response.json()['source'] == 'managed'
    assert not response.json()['push_configured']
