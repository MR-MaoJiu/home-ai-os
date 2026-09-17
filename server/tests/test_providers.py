import httpx
import pytest
from fastapi import HTTPException
from homeai.contracts import ProviderManifest
from homeai.providers import Registry
from homeai.security import Actor
from homeai.db import Secret


@pytest.mark.asyncio
async def test_provider_secret_scoped(system,alice):
    vault=system[0].state.vault
    with system[2]() as db:
        secret=Secret(id='key1',owner_id=alice.user_id,household_id='h1',provider_id='local.test',value=vault.seal('private-service-token',alice.user_id+':secret:key1'))
        db.add(secret);db.commit()
        seen=[]
        def transport(request):
            seen.append(request.headers.get('Authorization'))
            return httpx.Response(200,json={'ok':True})
        registry=Registry(vault,httpx.MockTransport(transport))
        manifest=ProviderManifest(id='local.test',version='1',adapter='http',endpoint='http://127.0.0.1:9999',allowed_hosts=['127.0.0.1'],capabilities={'document.parse@v1':'/invoke/parse'},secret_id='key1')
        actor=Actor(alice.user_id,'h1',alice.device_id,'adult')
        await registry.invoke(db,actor,manifest,'document.parse@v1',{},'inv1')
        assert seen==['Bearer private-service-token']
        with pytest.raises(HTTPException):
            await registry.invoke(db,Actor('other','h1','other-device','adult'),manifest,'document.parse@v1',{},'inv2')
        assert len(seen)==1


@pytest.mark.asyncio
async def test_provider_host_denied_before_network(system,alice):
    def never(request):raise AssertionError('不能发起请求')
    manifest=ProviderManifest(id='bad.test',version='1',adapter='http',endpoint='http://unexpected:80',allowed_hosts=['127.0.0.1'],capabilities={'document.parse@v1':'/parse'})
    registry=Registry(system[0].state.vault,httpx.MockTransport(never))
    with system[2]() as db:
        with pytest.raises(HTTPException):await registry.invoke(db,Actor(alice.user_id,'h1',alice.device_id,'adult'),manifest,'document.parse@v1',{},'inv1')
