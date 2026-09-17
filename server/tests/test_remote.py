import base64,json


def test_remote_binding_rejects_non_https_target(alice):
    code=base64.b64encode(json.dumps({'id':'instance','binding_token':'test-token','url':'javascript:alert(1)'}).encode()).decode()
    result=alice.request('POST','/api/v1/remote/bind',{'binding_code':code})
    assert result.status_code==422
    assert 'HTTPS' in result.json()['detail']
