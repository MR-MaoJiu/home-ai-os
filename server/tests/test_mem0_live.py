"""Mem0、真实本地 Embedding 与 Core 规范账本的集成验收。"""
import os,uuid
from pathlib import Path
import pytest
import httpx
from fastapi.testclient import TestClient
from homeai.api import create_app
from homeai.config import Settings
from homeai.contracts import ProviderManifest
from homeai.db import Provider,Secret,Principal,scope,uid
from homeai.derived_memory import reconcile_provider
from homeai.runtime import run_task
from conftest import SignedClient
from test_security_data import put

pytestmark=pytest.mark.skipif(os.environ.get('HOMEAI_MEM0_TEST')!='1',reason='需要真实 Mem0、Embedding、PostgreSQL、OPA')


@pytest.mark.asyncio
async def test_real_mem0_rebuild_isolation_delete_disable():
    settings=Settings();settings.database_url=settings.database_url.rsplit('/',1)[0]+'/homeai_test'
    app=create_app(settings);user=SignedClient(TestClient(app),app.state.db,household=str(uuid.uuid4()))
    provider_id='a.mem0.live';secret_id=uid();token=Path('state/provider-secrets/mem0.token').read_text().strip()
    with app.state.db() as db:
        principal=db.get(Principal,user.user_id);household=principal.household_id;scope(db,user.user_id,household)
        db.add(Secret(id=secret_id,owner_id=user.user_id,household_id=household,provider_id=provider_id,value=app.state.vault.seal(token,user.user_id+':secret:'+secret_id)))
        manifest=ProviderManifest(id=provider_id,version='1.0.11',adapter='http',endpoint='http://127.0.0.1:8101',allowed_hosts=['127.0.0.1'],secret_id=secret_id,capabilities={'memory.semantic.index@v1':'/invoke/index','memory.semantic.purge@v1':'/invoke/purge','memory.semantic.search@v1':'/invoke/search'})
        row=db.get(Provider,provider_id)
        if row:row.manifest,row.enabled=manifest.model_dump_json(),True
        else:db.add(Provider(id=provider_id,manifest=manifest.model_dump_json(),enabled=True))
        db.commit()
    try:
        rid=put(user,{'source':'validation','source_id':str(uuid.uuid4()),'kind':'memory.fact','version':1,'payload':{'content':'周末喜欢爬山'}})
        await reconcile_provider(app.state,user.user_id,household,provider_id)
        states=user.request('GET','/api/v1/memory/derived').json()
        assert next(x for x in states if x['provider_id']==provider_id)['status']=='READY',states
        assert user.request('POST','/api/v1/memory/derived/'+provider_id+'/rebuild').status_code==200
        await reconcile_provider(app.state,user.user_id,household,provider_id)
        response=user.request('POST','/api/v1/tasks',{'idempotency_key':str(uuid.uuid4()),'capability':'memory.semantic.search@v1','arguments':{'query':'户外运动'}})
        tid=response.json()['id'];await run_task(app.state,tid,user.user_id)
        task=user.request('GET','/api/v1/tasks/'+tid).json()
        assert task['status']=='SUCCEEDED',task
        assert task['result'][0]['id']==rid
        assert task['result'][0]['payload']['content']=='周末喜欢爬山'
        async with httpx.AsyncClient(trust_env=False,headers={'Authorization':'Bearer '+token}) as client:
            other=await client.post(manifest.endpoint+'/invoke/search',json={'subject_id':str(uuid.uuid4()),'invocation_id':str(uuid.uuid4()),'arguments':{'query':'户外运动'}})
            assert other.json()['canonical_ids']==[]
        with app.state.db() as db:
            db.get(Provider,provider_id).enabled=False;db.commit()
        await reconcile_provider(app.state,user.user_id,household,provider_id)
        assert user.request('GET','/api/v1/data/'+rid).status_code==200
        assert user.request('DELETE','/api/v1/data/'+rid).status_code==200
        with app.state.db() as db:
            db.get(Provider,provider_id).enabled=True;db.commit()
        await reconcile_provider(app.state,user.user_id,household,provider_id)
        async with httpx.AsyncClient(trust_env=False,headers={'Authorization':'Bearer '+token}) as client:
            result=await client.post(manifest.endpoint+'/invoke/search',json={'subject_id':user.user_id,'invocation_id':str(uuid.uuid4()),'arguments':{'query':'户外运动'}})
            assert result.status_code==200 and result.json()['canonical_ids']==[]
            health=(await client.get(manifest.endpoint+'/health')).json()
            assert health['egress']['allowed_connections']>0
            assert health['egress']['blocked_connections']==0
    finally:
        with app.state.db() as db:db.get(Provider,provider_id).enabled=False;db.commit()
