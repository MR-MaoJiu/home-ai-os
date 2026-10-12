"""验证信令队列调度顺序；测试替身只用于控制流程，不作为真实网络性能证据。"""
import asyncio
import time
import pytest
from types import SimpleNamespace
from homeai import connect_signalling
from homeai.config import Settings


async def test_existing_device_offer_is_answered_before_new_enrollment(system,monkeypatch):
    app=system[0]
    events=[]
    config={'enabled':True,'transport_policy':'direct_only','protocol':1,
        'instance_id':'fixture-instance','credential':'fixture-credential','portal_url':'https://fixture.invalid','household_id':'h1'}
    class Broker:
        def __init__(self,*args):pass
        async def request(self,method,path,body=None):
            events.append((method,path))
            if path=='/api/direct/permit':return {'expires':time.time()+300}
            if path=='/api/direct/offers':return [{'device_id':'fixture-device','envelope':{'payload':{'session':'fixture-session','expires':time.time()+60}}}]
            if path.startswith('/api/direct/answers/'):return {}
            if path=='/api/direct/enrollments':raise asyncio.CancelledError()
            raise AssertionError(path)
    class Manager:
        platform_expiry=0
        async def close_source(self,*args,**kwargs):pass
        async def negotiate(self,*args,**kwargs):
            events.append(('local','negotiate'))
            return {'fixture':'answer'}
    from homeai import direct_sessions
    monkeypatch.setattr(connect_signalling,'read_config',lambda app:config)
    monkeypatch.setattr(connect_signalling,'effective_stun',lambda *args:[])
    monkeypatch.setattr(connect_signalling,'Broker',Broker)
    monkeypatch.setattr(direct_sessions,'sessions',lambda app:Manager())
    with pytest.raises(asyncio.CancelledError):await connect_signalling.run_agent(app)
    assert events.index(('POST','/api/direct/answers/fixture-session')) < events.index(('GET','/api/direct/enrollments'))
    assert not hasattr(app.state,'connect_http_client')


def test_signalling_poll_interval_is_bounded():
    assert Settings().direct_signalling_poll_seconds==1
    for value in (0,-1,0.1,6):
        with pytest.raises(ValueError):Settings(direct_signalling_poll_seconds=value)
