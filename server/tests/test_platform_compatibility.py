"""协议拒绝边界测试，不模拟第三方平台已完成连接。"""
import pytest

from homeai.connect_signalling import platform_origin, validate_capabilities


@pytest.mark.parametrize('url', [
    'http://connect.example', 'https://user:secret@connect.example',
    'https://connect.example/api', 'https://connect.example?token=x',
    'https://connect.example#fragment', 'https://connect.example:0',
    'https://connect.example:99999', 'https://connect.example\\other',
    ' https://connect.example',
])
def test_reject_invalid_platform_origin(url):
    with pytest.raises(ValueError):
        platform_origin(url)


def test_third_party_origin_is_not_restricted_to_official_domain():
    assert platform_origin('https://connect.example:9443/') == 'https://connect.example:9443'


@pytest.mark.parametrize('value', [
    [], {}, {'protocol': True, 'transport_policy': 'direct_only'},
    {'protocol': 2, 'transport_policy': 'direct_only'},
    {'protocol': 1, 'transport_policy': 'relay'},
    {'protocol': 1, 'transport_policy': 'direct_only', 'stun_urls': ['turn:example:3478']},
])
def test_reject_incompatible_capabilities(value):
    with pytest.raises(ValueError):
        validate_capabilities(value)


def test_platform_check_requires_family_owner(system, alice):
    from conftest import SignedClient
    path = '/api/v1/remote/platform-check'
    assert system[1].post(path, json={}).status_code == 401
    member = SignedClient(system[1], system[2])
    assert member.request('POST', path, {}).status_code == 403
    assert alice.request('POST', path, {'portal_url': 'https://example.test/api'}).status_code == 422


def test_family_custom_stun_does_not_change_platform_binding(system,alice):
    from homeai.private_files import private_write
    from homeai.connect_signalling import effective_stun
    from homeai.remote import read_config
    app=system[0].state
    config={'protocol':1,'transport_policy':'direct_only','household_id':'h1','enabled':True,'portal_url':'https://portal.example','credential':'test-only','stun_urls':['stun:platform.example:3478']}
    private_write(app.settings.state_dir/'remote-config.enc',app.vault.seal(config,'remote-config'))
    path='/api/v1/remote/stun'
    assert alice.request('PUT',path,{'mode':'custom','stun_urls':['turn:example:3478']}).status_code==422
    assert alice.request('PUT',path,{'mode':'custom','stun_urls':['stun:custom.example:3478']}).status_code==200
    assert effective_stun(app,config)==['stun:custom.example:3478']
    assert read_config(app)==config
    assert alice.request('PUT',path,{'mode':'platform','stun_urls':[]}).status_code==200
    assert effective_stun(app,config)==['stun:platform.example:3478']
