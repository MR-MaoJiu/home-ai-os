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
