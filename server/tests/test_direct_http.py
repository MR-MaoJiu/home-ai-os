import pytest
from homeai.direct_http import validate_request


def request(target='/api/v1/me', headers=None):
    return {'wire_version': 3, 'kind': 'request', 'id': 'a' * 32, 'method': 'GET', 'target': target, 'headers': headers or {}, 'size': 0}


@pytest.mark.parametrize('target', ['https://evil.example/api/v1/me', '//evil.example/api/v1/me', '/api/v1/../../admin/', '/api/v1/pair', '/api/v1/browser/login', '/admin/', '/api/v1/me#fragment'])
def test_direct_cannot_proxy_arbitrary_destinations(target):
    with pytest.raises(ValueError):
        validate_request(request(target))


@pytest.mark.parametrize('headers', [{'cookie': 'session=invalid'}, {'host': 'evil.example'}, {'x-forwarded-for': '127.0.0.1'}, {'authorization': 'Bearer a\r\nx: y'}])
def test_direct_cannot_use_browser_or_proxy_headers(headers):
    with pytest.raises(ValueError):
        validate_request(request(headers=headers))


@pytest.mark.parametrize('version', [1, 2, True, '3', None])
def test_reject_old_or_ambiguous_framing(version):
    value=request();value['wire_version']=version
    with pytest.raises(ValueError):validate_request(value)


@pytest.mark.asyncio
async def test_body_delay_uses_server_receipt_time_but_rechecks_revocation(system, alice):
    import time
    import uuid
    from starlette.requests import Request
    from fastapi import HTTPException
    from homeai.crypto import digest
    from homeai.security import authenticate
    from homeai.db import Device
    issued = time.time()-70
    def headers():
        timestamp,nonce=str(issued),str(uuid.uuid4())
        proof='\n'.join([timestamp,nonce,'GET','/api/v1/me',digest(b''),digest(alice.token.encode())])
        return {'authorization':'Bearer '+alice.token,'x-homeai-time':timestamp,'x-homeai-nonce':nonce,
                'x-homeai-signature':alice.sign(proof.encode()),'x-homeai-received-at':str(issued)}
    def received_request():
        return Request({'type':'http','method':'GET','path':'/api/v1/me','query_string':b'',
                        'scheme':'https','server':('test',443),'headers':[(k.encode(),v.encode()) for k,v in headers().items()],
                        'app':system[0],'homeai.body_digest':digest(b''),'homeai.received_at':issued})
    assert (await authenticate(received_request())).device_id==alice.device_id
    # 外部请求不能通过伪造同名请求头延长时间窗。
    assert alice.client.get('/api/v1/me',headers=headers()).status_code==401
    with system[2]() as db:
        db.get(Device,alice.device_id).revoked=True;db.commit()
    with pytest.raises(HTTPException) as error:
        await authenticate(received_request())
    assert error.value.status_code==401
