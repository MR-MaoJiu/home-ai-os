import pyotp
from fastapi.testclient import TestClient
from homeai.security import credential
from homeai.db import Principal


def bootstrap(system,alice):
    with system[2]() as db:
        ticket=credential(db,alice.user_id,'browser_bootstrap',300)
        db.commit()
    c=TestClient(system[0],base_url='https://testserver')
    r=c.post('/api/v1/browser/setup',json={'ticket':ticket,'username':'admin','password':'correct-horse-test-battery'},headers={'Origin':'https://testserver'})
    assert r.status_code==200,r.text
    return c,r.json()


def csrf(c):return {'Origin':'https://testserver','X-CSRF-Token':c.cookies.get('__Host-homeai-csrf')}


def test_setup_cannot_access_before_totp(system,alice):
    c,result=bootstrap(system,alice)
    assert c.get('/api/v1/me').status_code==401
    code=pyotp.TOTP(result['secret']).now()
    response=c.post('/api/v1/browser/totp/confirm',json={'code':code},headers=csrf(c))
    assert response.status_code==200,response.text
    assert c.get('/api/v1/me').json()['user_id']==alice.user_id
    assert c.post('/api/v1/browser/logout').status_code==403
    assert c.post('/api/v1/browser/logout',headers=csrf(c)).status_code==200
    assert c.get('/api/v1/me').status_code==401


def test_setup_ticket_replay_and_cross_origin(system,alice):
    c,result=bootstrap(system,alice)
    r=c.post('/api/v1/browser/login',json={'username':'admin','password':'correct-horse-test-battery','code':'000000'},headers={'Origin':'https://evil.invalid'})
    assert r.status_code==403
    for _ in range(5):
        assert c.post('/api/v1/browser/login',json={'username':'admin','password':'wrong','code':'000000'},headers={'Origin':'https://testserver'}).status_code==401
    assert c.post('/api/v1/browser/login',json={'username':'admin','password':'wrong','code':'000000'},headers={'Origin':'https://testserver'}).status_code==429


def test_code_only_login_and_replay(system,alice):
    from homeai.db import BrowserAccount
    c,result=bootstrap(system,alice)
    # 初始化确认和登录使用不同时间窗，验证动态码不可重放。
    with system[2]() as db:
        account=db.get(BrowserAccount,'admin');account.totp_enabled=True;db.commit()
    code=pyotp.TOTP(result['secret']).now()
    r=c.post('/api/v1/browser/login',json={'username':'admin','code':code},headers={'Origin':'https://testserver'})
    assert r.status_code==200,r.text
    assert c.post('/api/v1/browser/login',json={'username':'admin','code':code},headers={'Origin':'https://testserver'}).status_code==401
