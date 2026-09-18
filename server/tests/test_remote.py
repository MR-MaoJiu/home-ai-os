import json


def test_legacy_binding_cannot_reenable_relay(system, alice):
    from homeai.private_files import private_write
    app = system[0].state
    config = {'enabled': True, 'url': 'https://old.example.test', 'credential': 'must-not-leak'}
    private_write(app.settings.state_dir/'remote-config.enc', app.vault.seal(config, 'remote-config'))
    # 即使历史状态声称租约有效，当前 API 也不将其解释为直连可用。
    private_write(app.settings.state_dir/'remote-status.json', json.dumps({'state': 'lease_active'}))
    status = alice.request('GET', '/api/v1/remote/status').json()
    assert status['enabled'] is False
    assert status['runtime']['state'] == 'direct_not_ready'
    assert status['legacy_runtime']['state'] == 'lease_active'
    assert status['direct_ready'] is False and status['relay_allowed'] is False
    assert status['transport_policy'] == 'direct_only'
    assert status['previously_enabled'] is True and status['migration_required'] is True
    assert 'must-not-leak' not in json.dumps(status)
    for path in ('enable', 'use-managed-https'):
        assert alice.request('POST', '/api/v1/remote/'+path).status_code == 404
    assert alice.request('POST', '/api/v1/remote/disable').json() == {'enabled': False}
    assert alice.request('GET', '/api/v1/remote/status').json()['previously_enabled'] is False


def test_removed_remote_routes_are_not_published(system, alice):
    removed = ('request-code', 'bind', 'enable', 'use-managed-https')
    paths = system[0].openapi()['paths']
    for suffix in removed:
        path = '/api/v1/remote/' + suffix
        assert path not in paths
        assert alice.request('GET' if suffix == 'request-code' else 'POST', path).status_code == 404
