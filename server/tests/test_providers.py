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


def test_document_external_relationship_cannot_bypass_with_quotes():
    import io,zipfile
    from homeai_providers.docling_adapter import preflight
    for quote in ('"', "'"):
        data=io.BytesIO()
        with zipfile.ZipFile(data,'w') as archive:
            archive.writestr('_rels/.rels', f'<Relationships><Relationship TargetMode={quote}External{quote} Target={quote}file:///private/data{quote}/></Relationships>')
        with pytest.raises(HTTPException):
            preflight(data.getvalue(),'.docx')


def test_registry_skips_other_members_credentials(system,alice):
    from homeai.db import Provider,scope
    vault=system[0].state.vault
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        for provider_id,owner in [('a.other','someone-else'),('b.mine',alice.user_id)]:
            secret_id=provider_id+'.secret'
            db.add(Secret(id=secret_id,owner_id=owner,household_id='h1',provider_id=provider_id,value=vault.seal('test-token',owner+':secret:'+secret_id)))
            manifest=ProviderManifest(id=provider_id,version='1',adapter='http',endpoint='http://127.0.0.1:8103',allowed_hosts=['127.0.0.1'],capabilities={'document.parse@v1':'/invoke/parse'},secret_id=secret_id)
            db.add(Provider(id=provider_id,manifest=manifest.model_dump_json(),enabled=True))
        db.commit()
        assert Registry(vault).resolve(db,'document.parse@v1').id=='b.mine'


def test_mem0_python_egress_guard_rejects_external_destinations():
    import subprocess,sys,os
    code = '''import socket
from homeai_providers.egress_guard import install_mem0_guard,stats
install_mem0_guard()
socket.getaddrinfo(b"127.0.0.1",58081)
for operation in (lambda:socket.getaddrinfo("example.com",443),lambda:socket.socket().connect(("203.0.113.1",443))):
    try:operation()
    except PermissionError:pass
    else:raise AssertionError("未阻止外部目的地")
assert stats["blocked_connections"]==2
'''
    subprocess.run([sys.executable,'-c',code],env={**os.environ,'PYTHONPATH':'providers'},check=True)
