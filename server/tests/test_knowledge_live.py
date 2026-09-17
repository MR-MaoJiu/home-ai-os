"""真实 Docling 文档、Embedding 和 PostgreSQL 分块检索验收。"""
import os,uuid
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from homeai.api import create_app
from homeai.config import Settings
from homeai.contracts import ProviderManifest
from homeai.db import Provider,Secret,Principal,scope,uid
from homeai.runtime import run_task
from homeai.knowledge import reconcile
from conftest import SignedClient
from test_docling_live import upload,docx_bytes

pytestmark=pytest.mark.skipif(os.environ.get('HOMEAI_KNOWLEDGE_TEST')!='1',reason='需要真实 Docling、Embedding、PostgreSQL 和 OPA')


@pytest.mark.asyncio
async def test_real_document_search_shared_revoked_and_deleted():
    settings=Settings();settings.database_url=settings.database_url.rsplit('/',1)[0]+'/homeai_test'
    app=create_app(settings);client=TestClient(app);household=str(uuid.uuid4())
    user=SignedClient(client,app.state.db,household=household);other=SignedClient(client,app.state.db,household=household)
    secret_id=uid();parse_id='a.knowledge.docling';embed_id='a.knowledge.embed'
    with app.state.db() as db:
        scope(db,user.user_id,household)
        db.add(Secret(id=secret_id,owner_id=user.user_id,household_id=household,provider_id=parse_id,value=app.state.vault.seal(Path('state/provider-secrets/docling.token').read_text().strip(),user.user_id+':secret:'+secret_id)))
        manifests=[ProviderManifest(id=parse_id,version='2.128.0',adapter='http',endpoint='http://127.0.0.1:8103',allowed_hosts=['127.0.0.1'],secret_id=secret_id,capabilities={'document.parse@v1':'/invoke/parse'},timeout_seconds=300),ProviderManifest(id=embed_id,version='0f741b5a6585bd53aeb15cd1372c56f2a0f65e12',adapter='openai',endpoint='http://127.0.0.1:58081/v1',model='embeddinggemma-300M-Q8_0.gguf',allowed_hosts=['127.0.0.1'],embedding_document_prefix='title: none | text: ',embedding_query_prefix='task: search result | query: ',capabilities={'model.embed@v1':'embed'})]
        for manifest in manifests:
            row=db.get(Provider,manifest.id)
            if row:row.manifest,row.enabled=manifest.model_dump_json(),True
            else:db.add(Provider(id=manifest.id,manifest=manifest.model_dump_json(),enabled=True))
        db.commit()
    if os.environ.get('HOMEAI_MODEL_TEST')=='1':
        with app.state.db() as db:
            model=ProviderManifest(id='a.knowledge.agent',version='bc640142c66e1fdd12af0bd68f40445458f3869b',adapter='openai',endpoint='http://127.0.0.1:58082/v1',model='Qwen3-4B-Q4_K_M.gguf',allowed_hosts=['127.0.0.1'],capabilities={'model.generate@v1':'chat'})
            row=db.get(Provider,model.id)
            if row:row.manifest,row.enabled=model.model_dump_json(),True
            else:db.add(Provider(id=model.id,manifest=model.model_dump_json(),enabled=True))
            db.commit()
    try:
        original=upload(user,'会议.docx',docx_bytes())
        task=user.request('POST','/api/v1/files/'+original+'/parse').json()['id']
        await run_task(app.state,task,user.user_id)
        parsed=user.request('GET','/api/v1/tasks/'+task).json()
        assert parsed['status']=='SUCCEEDED',parsed
        rid=parsed['result']['record_id']
        await reconcile(app.state,user.user_id,household)
        response=user.request('POST','/api/v1/knowledge/search',{'query':'何时检查备份'})
        assert response.status_code==200,response.text
        result=response.json();assert result['mode']=='pgvector_chunks',result
        assert result['matches'][0]['record_id']==rid
        assert '三点' in result['matches'][0]['excerpt']
        if os.environ.get('HOMEAI_MODEL_TEST')=='1':
            response=user.request('POST','/api/v1/tasks',{'idempotency_key':str(uuid.uuid4()),'message':'我的会议文档里约定哪天几点检查备份？请先检索文档再回答。/no_think','max_steps':4,'max_output_tokens':512})
            agent_id=response.json()['id']
            for _ in range(10):
                await run_task(app.state,agent_id,user.user_id)
                answer=user.request('GET','/api/v1/tasks/'+agent_id).json()
                if answer['status'] in {'SUCCEEDED','FAILED','CANCELED'}:break
            assert answer['status']=='SUCCEEDED',answer
            message=answer['result']['choices'][0]['message']['content']
            assert any(day in message for day in ('周六','星期六')),message
            assert any(hour in message for hour in ('三点','3点','3:00','15:00')),message
            steps=user.request('GET','/api/v1/tasks/'+agent_id+'/steps').json()
            assert any(step['capability']=='knowledge.search@v1' for step in steps),steps
            assert any(source['record_id']==rid for source in answer['result']['sources'])

        assert other.request('POST','/api/v1/knowledge/search',{'query':'何时检查备份'}).json()['matches']==[]
        assert user.request('PUT',f'/api/v1/data/{rid}/grants/{other.user_id}').status_code==200
        assert other.request('POST','/api/v1/knowledge/search',{'query':'何时检查备份'}).json()['matches'][0]['record_id']==rid
        cached_task=other.request('POST','/api/v1/tasks',{'idempotency_key':str(uuid.uuid4()),'capability':'knowledge.search@v1','arguments':{'query':'备份时间'}}).json()['id']
        await run_task(app.state,cached_task,other.user_id)
        assert user.request('DELETE',f'/api/v1/data/{rid}/grants/{other.user_id}').status_code==200
        cached=other.request('GET','/api/v1/tasks/'+cached_task).json()
        assert cached['result'] is None and cached['result_redacted'] is True
        assert other.request('GET','/api/v1/tasks/'+cached_task+'/steps').json()==[]
        assert other.request('POST','/api/v1/knowledge/search',{'query':'何时检查备份'}).json()['matches']==[]
        assert user.request('DELETE','/api/v1/data/'+original).status_code==200
        assert user.request('POST','/api/v1/knowledge/search',{'query':'何时检查备份'}).json()['matches']==[]
    finally:
        with app.state.db() as db:
            for identifier in (parse_id,embed_id,'a.knowledge.agent'):
                row=db.get(Provider,identifier)
                if row:row.enabled=False
            db.commit()
