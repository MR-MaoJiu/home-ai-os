"""真实重排服务、规范账本与权限检查；外部模型不使用替身。"""
import os
import uuid
from pathlib import Path
import httpx
import pytest
from fastapi import HTTPException
from homeai.contracts import ProviderManifest
from homeai.db import Provider, Secret, Principal, scope, uid
from homeai.knowledge import reconcile
from homeai.reranking import ordered_indices
from homeai.runtime import run_task
from test_security_data import put
from test_workflows import workflow
from conftest import SignedClient


def test_rerank_response_cannot_insert_duplicate_or_foreign_candidates():
    assert ordered_indices({'results':[{'index':0,'relevance_score':0.1},{'index':1,'relevance_score':0.8}]},2)==[1,0]
    for rows in [[],[{'index':2,'relevance_score':0.5}],
                 [{'index':True,'relevance_score':0.5}],
                 [{'index':0,'relevance_score':float('nan')}],
                 [{'index':0,'relevance_score':1.1}],
                 [{'index':0,'relevance_score':0.5},{'index':0,'relevance_score':0.6}]]:
        with pytest.raises(HTTPException): ordered_indices({'results':rows},2 if len(rows)==2 else 1)


@pytest.mark.skipif(os.getenv('HOMEAI_RERANKER_TEST')!='1',reason='需要运行真实 Reranker')
def test_real_multilingual_reranking_and_authentication():
    token=Path('state/provider-secrets/reranker.token').read_text().strip()
    with httpx.Client(trust_env=False,timeout=180) as client:
        assert client.get('http://127.0.0.1:8109/health').status_code==401
        for query,documents in [
            ('什么时候检查备份？',['周日购买水果和牛奶。','星期六下午三点检查服务器备份。','明天晚上给花浇水。']),
            ('When should I check the backup?',['Buy milk on Sunday.','Check the server backup at three on Saturday.','Water the flowers tomorrow.'])]:
            response=client.post('http://127.0.0.1:8109/invoke/rerank',headers={'Authorization':'Bearer '+token},
                json={'subject_id':'integration-reranker','invocation_id':str(uuid.uuid4()),'arguments':{'query':query,'documents':documents}})
            assert response.status_code==200,response.text
            assert ordered_indices(response.json(),3)[0]==1,response.text
        health=client.get('http://127.0.0.1:8109/health',headers={'Authorization':'Bearer '+token}).json()
        assert health['egress']['allowed_connections']==0


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv('HOMEAI_RERANKER_TEST')!='1' or os.getenv('HOMEAI_INTEGRATION')!='1',reason='需要真实重排、Embedding、PostgreSQL 与 OPA')
async def test_real_knowledge_reranking_revocation_and_failure(workflow):
    app,user=workflow
    token=Path('state/provider-secrets/reranker.token').read_text().strip()
    provider_id='a-reranker.'+uuid.uuid4().hex
    embed_id='a-rerank-embed.'+uuid.uuid4().hex
    with app.db() as db:
        principal=db.get(Principal,user.user_id);household=principal.household_id
        scope(db,user.user_id,household);secret_id=uid()
        db.add(Secret(id=secret_id,owner_id=user.user_id,household_id=household,provider_id=provider_id,
            value=app.vault.seal(token,user.user_id+':secret:'+secret_id)))
        manifest=ProviderManifest(id=provider_id,version='2cfc18c9415c912f9d8155881c133215df768a70',adapter='http',
            endpoint='http://127.0.0.1:8109',allowed_hosts=['127.0.0.1'],secret_id=secret_id,
            capabilities={'model.rerank@v1':'/invoke/rerank'},timeout_seconds=180)
        embed=ProviderManifest(id=embed_id,version='0f741b5a6585bd53aeb15cd1372c56f2a0f65e12',adapter='openai',
            endpoint='http://127.0.0.1:58081/v1',model='embeddinggemma-300M-Q8_0.gguf',allowed_hosts=['127.0.0.1'],
            embedding_query_prefix='task: search result | query: ',embedding_document_prefix='title: none | text: ',
            capabilities={'model.embed@v1':'embed'})
        for item in (manifest,embed):db.add(Provider(id=item.id,manifest=item.model_dump_json(),enabled=True))
        db.commit()
    try:
        empty=user.request('POST','/api/v1/knowledge/search',{'query':'什么时候检查备份？'}).json()
        assert empty['matches']==[] and empty['reranking']=='not_needed',empty
        records=[]
        for content in ['周日采购苹果和牛奶。','星期六下午三点检查服务器备份。','明天给花浇水。']:
            records.append(put(user,{'source':'integration','source_id':str(uuid.uuid4()),'kind':'document.parsed',
                'version':1,'payload':{'name':'验收文档','markdown':content}}))
        await reconcile(app,user.user_id,household)
        response=user.request('POST','/api/v1/knowledge/search',{'query':'什么时候检查备份？'})
        assert response.status_code==200,response.text
        result=response.json()
        assert result['mode']=='pgvector_chunks' and result['reranking']=='applied',result
        assert result['matches'][0]['record_id']==records[1]
        created=user.request('POST','/api/v1/tasks',{'capability':'knowledge.search@v1',
            'arguments':{'query':'什么时候检查备份？'},'idempotency_key':str(uuid.uuid4())})
        assert created.status_code==202,created.text
        task_id=created.json()['id']
        await run_task(app,task_id,user.user_id)
        cached=user.request('GET','/api/v1/tasks/'+task_id).json()
        assert cached['status']=='SUCCEEDED' and cached['result']['reranking']=='applied',cached
        other=SignedClient(user.client,app.db,household=household)
        assert other.request('POST','/api/v1/knowledge/search',{'query':'什么时候检查备份？'}).json()['matches']==[]
        assert user.request('PUT',f'/api/v1/data/{records[1]}/grants/{other.user_id}').status_code==200
        assert other.request('POST','/api/v1/knowledge/search',{'query':'什么时候检查备份？'}).json()['matches'][0]['record_id']==records[1]
        assert user.request('DELETE',f'/api/v1/data/{records[1]}/grants/{other.user_id}').status_code==200
        assert other.request('POST','/api/v1/knowledge/search',{'query':'什么时候检查备份？'}).json()['matches']==[]
        # 真实错误端点返回拒绝；降级不能伪装成模型重排成功。
        with app.db() as db:
            broken=manifest.model_copy(update={'endpoint':'http://127.0.0.1:8109/unavailable'})
            db.get(Provider,provider_id).manifest=broken.model_dump_json();db.commit()
        result=user.request('POST','/api/v1/knowledge/search',{'query':'什么时候检查备份？'}).json()
        assert result['reranking']=='unavailable' and result['matches'],result
        with app.db() as db:
            row=db.get(Provider,provider_id);row.manifest=manifest.model_dump_json();row.enabled=False;db.commit()
        assert user.request('POST','/api/v1/knowledge/search',{'query':'什么时候检查备份？'}).json()['reranking']=='not_configured'
        assert user.request('DELETE','/api/v1/data/'+records[1]).status_code==200
        cached=user.request('GET','/api/v1/tasks/'+task_id).json()
        assert cached['result'] is None and cached['result_redacted'] is True,cached
        assert all(item['record_id']!=records[1] for item in user.request('POST','/api/v1/knowledge/search',{'query':'什么时候检查备份？'}).json()['matches'])
    finally:
        with app.db() as db:
            for identifier in (provider_id,embed_id):db.get(Provider,identifier).enabled=False
            db.commit()
