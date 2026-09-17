"""真实 Graphiti、Neo4j、本地模型与规范账本的集成验证。"""
import os,uuid
from pathlib import Path
import pytest,httpx
from fastapi.testclient import TestClient
from homeai.api import create_app
from homeai.config import Settings
from homeai.contracts import ProviderManifest
from homeai.db import Provider,Secret,Principal,scope,uid
from homeai.derived_memory import reconcile_provider
from homeai.runtime import run_task
from conftest import SignedClient
from test_security_data import put

pytestmark=pytest.mark.skipif(os.environ.get('HOMEAI_GRAPHITI_TEST')!='1',reason='需要真实 Graphiti、Neo4j、4B 模型、Embedding、PostgreSQL 与 OPA')


@pytest.mark.asyncio
async def test_real_graphiti_canonical_search_and_deletion():
    settings=Settings();settings.database_url=settings.database_url.rsplit('/',1)[0]+'/homeai_test'
    app=create_app(settings);user=SignedClient(TestClient(app),app.state.db,household=str(uuid.uuid4()))
    provider_id='a.graphiti.live';secret_id=uid();token=Path('state/provider-secrets/graphiti.token').read_text().strip()
    with app.state.db() as db:
        principal=db.get(Principal,user.user_id);household=principal.household_id;scope(db,user.user_id,household)
        db.add(Secret(id=secret_id,owner_id=user.user_id,household_id=household,provider_id=provider_id,value=app.state.vault.seal(token,user.user_id+':secret:'+secret_id)))
        manifest=ProviderManifest(id=provider_id,version='0.30.2',adapter='http',endpoint='http://127.0.0.1:8102',allowed_hosts=['127.0.0.1'],secret_id=secret_id,timeout_seconds=300,capabilities={'memory.graph.index@v1':'/invoke/index','memory.graph.purge@v1':'/invoke/purge','memory.graph.search@v1':'/invoke/search'})
        row=db.get(Provider,provider_id)
        if row:row.manifest,row.enabled=manifest.model_dump_json(),True
        else:db.add(Provider(id=provider_id,manifest=manifest.model_dump_json(),enabled=True))
        db.commit()
    try:
        rid=put(user,{'source':'validation','source_id':str(uuid.uuid4()),'kind':'memory.fact','version':1,'payload':{'content':'Alice lives in Paris. Alice works as a teacher.'}})
        await reconcile_provider(app.state,user.user_id,household,provider_id)
        states=user.request('GET','/api/v1/memory/derived').json()
        assert next(x for x in states if x['provider_id']==provider_id)['status']=='READY',states
        response=user.request('POST','/api/v1/tasks',{'idempotency_key':str(uuid.uuid4()),'capability':'memory.graph.search@v1','arguments':{'query':'Where does Alice live?'},'step_timeout_seconds':300})
        tid=response.json()['id'];await run_task(app.state,tid,user.user_id)
        task=user.request('GET','/api/v1/tasks/'+tid).json()
        assert task['status']=='SUCCEEDED',task
        assert any(row['id']==rid and row['payload']['content']=='Alice lives in Paris. Alice works as a teacher.' for row in task['result']),task
        async with httpx.AsyncClient(trust_env=False,headers={'Authorization':'Bearer '+token},timeout=120) as client:
            other=await client.post(manifest.endpoint+'/invoke/search',json={'subject_id':str(uuid.uuid4()),'invocation_id':str(uuid.uuid4()),'arguments':{'query':'Where does Alice live?'}})
            other.raise_for_status();assert other.json()['canonical_ids']==[]
        assert user.request('DELETE','/api/v1/data/'+rid).status_code==200
        await reconcile_provider(app.state,user.user_id,household,provider_id)
        async with httpx.AsyncClient(trust_env=False,headers={'Authorization':'Bearer '+token},timeout=120) as client:
            result=await client.post(manifest.endpoint+'/invoke/search',json={'subject_id':user.user_id,'invocation_id':str(uuid.uuid4()),'arguments':{'query':'Where does Alice live?'}})
            result.raise_for_status();assert result.json()['canonical_ids']==[]
            health=(await client.get(manifest.endpoint+'/health')).json()
            assert health['egress']['allowed_connections']>0 and health['egress']['blocked_connections']==0
    finally:
        with app.state.db() as db:db.get(Provider,provider_id).enabled=False;db.commit()
