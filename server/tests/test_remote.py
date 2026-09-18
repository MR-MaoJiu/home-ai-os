import base64,json


def test_remote_binding_rejects_non_https_target(alice):
    code=base64.b64encode(json.dumps({'id':'instance','binding_token':'test-token','url':'javascript:alert(1)'}).encode()).decode()
    result=alice.request('POST','/api/v1/remote/bind',{'binding_code':code})
    assert result.status_code==422
    assert 'HTTPS' in result.json()['detail']


def test_remote_only_accepts_configured_https_port(alice):
    response=alice.request('POST','/api/v1/remote/bind',{'binding_code':'x'*40,'local_https_port':59001})
    assert response.status_code==422
    assert '受管 HTTPS 端口' in response.json()['detail']
    status=alice.request('GET','/api/v1/remote/status').json()
    assert status['managed_https_port']==58448 and status['tls']['status']=='not_running'
    assert alice.request('POST','/api/v1/remote/use-managed-https').status_code==409


def test_legacy_binding_cannot_reenable_relay(system, alice):
    from homeai.remote import private_write
    app = system[0].state
    config = {'enabled': True, 'url': 'https://old.example.test', 'credential': 'must-not-leak'}
    private_write(app.settings.state_dir/'remote-config.enc', app.vault.seal(config, 'remote-config'))
    # 即使历史状态声称租约有效，当前 API 也不将其解释为直连可用。
    private_write(app.settings.state_dir/'remote-status.json', json.dumps({'state': 'lease_active'}))
    status = alice.request('GET', '/api/v1/remote/status').json()
    assert status['enabled'] is False
    assert status['direct_ready'] is False and status['relay_allowed'] is False
    assert status['transport_policy'] == 'direct_only'
    assert status['previously_enabled'] is True and status['migration_required'] is True
    assert 'must-not-leak' not in json.dumps(status)
    for path in ('enable', 'use-managed-https'):
        assert alice.request('POST', '/api/v1/remote/'+path).status_code == 409
    assert alice.request('POST', '/api/v1/remote/disable').json() == {'enabled': False}
    assert alice.request('GET', '/api/v1/remote/status').json()['previously_enabled'] is False


def test_valid_legacy_binding_rejected_before_platform_request(alice):
    code = base64.b64encode(json.dumps({
        'id': 'old-instance', 'binding_token': 'old-token',
        'url': 'https://old.example.test',
    }).encode()).decode()
    # 使用不能解析的平台地址；正确行为是在发出请求之前拒绝旧协议。
    result = alice.request('POST', '/api/v1/remote/bind', {
        'binding_code': code, 'portal_url': 'https://no-platform.invalid',
    })
    assert result.status_code == 409
    assert '纯直连' in result.json()['detail']


def test_migration_agent_reports_no_transport(system):
    from homeai.remote import private_write
    from homeai.remote_agent import write_migration_status
    app = system[0].state
    assert write_migration_status(app)['state'] == 'disabled'
    private_write(app.settings.state_dir/'remote-config.enc', app.vault.seal({'enabled': True}, 'remote-config'))
    result = write_migration_status(app)
    assert result['state'] == 'direct_not_ready'
    assert result['process_running'] is False and result['relay_allowed'] is False
    assert json.loads((app.settings.state_dir/'remote-status.json').read_text()) == result
