import os
from datetime import datetime, timezone
from functools import lru_cache
from urllib.parse import urlparse
from fastapi import HTTPException
from importlib.metadata import version


@lru_cache
def graph():
    if version('graphiti-core') != '0.30.2' or os.environ.get('GRAPHITI_TELEMETRY_ENABLED', '').lower() != 'false':
        raise RuntimeError('Graphiti 必须使用固定版本并关闭遥测')
    from graphiti_core import Graphiti
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
    from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
    from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
    base = os.environ['LOCAL_MODEL_URL']
    embed_base = os.environ['LOCAL_EMBEDDING_URL']
    for endpoint in (base, embed_base):
        url = urlparse(endpoint)
        if url.scheme != 'http' or url.hostname != '127.0.0.1' or url.username or url.password or url.query or url.fragment:
            raise RuntimeError('Graphiti 只允许显式本地模型端点')
    database = urlparse(os.environ['NEO4J_URI'])
    if database.scheme != 'bolt' or database.hostname != '127.0.0.1' or database.port != 57687:
        raise RuntimeError('Graphiti 只能连接独立本地图数据库')
    from openai import AsyncOpenAI, DefaultAsyncHttpxClient
    model_client = AsyncOpenAI(api_key='local-only', base_url=base, http_client=DefaultAsyncHttpxClient(trust_env=False), timeout=120, max_retries=1)
    embed_client = AsyncOpenAI(api_key='local-only', base_url=embed_base, http_client=DefaultAsyncHttpxClient(trust_env=False), timeout=60, max_retries=1)
    config = LLMConfig(api_key='local-only', model=os.environ['LOCAL_MODEL_NAME'], small_model=os.environ['LOCAL_MODEL_NAME'], base_url=base, temperature=0)
    return Graphiti(os.environ['NEO4J_URI'], os.environ['NEO4J_USER'], os.environ['NEO4J_PASSWORD'],
        llm_client=OpenAIGenericClient(config=config, client=model_client, max_tokens=2048),
        embedder=OpenAIEmbedder(OpenAIEmbedderConfig(api_key='local-only',base_url=embed_base,embedding_model=os.environ['LOCAL_EMBEDDING_MODEL'],embedding_dim=int(os.environ['LOCAL_EMBEDDING_DIM'])), client=embed_client),
        cross_encoder=OpenAIRerankerClient(config=config, client=model_client),
        store_raw_episode_content=False)



_indexes_ready = False


async def ensure_indexes():
    global _indexes_ready
    client = graph()
    if not _indexes_ready:
        # 固定 SDK 会自动创建索引；等待该任务，避免并发重复创建。
        initial = getattr(client.driver, '_init_task', None)
        if initial is not None:
            try:
                await initial
            except Exception:
                await client.build_indices_and_constraints()
        else:
            await client.build_indices_and_constraints()
        _indexes_ready = True
    return client


async def operation(name, call):
    client = await ensure_indexes()
    if name == 'index':
        from graphiti_core.nodes import EpisodeType
        episode = await client.add_episode(name=call.arguments['record_id'], episode_body=call.arguments['content'], source=EpisodeType.text, source_description='Home AI canonical memory', reference_time=datetime.fromtimestamp(float(call.arguments['reference_time']), timezone.utc), group_id=call.subject_id)
        return {'episode_id': episode.episode.uuid, 'canonical_id': call.arguments['record_id']}
    if name == 'search':
        edges = await client.search(call.arguments['query'], group_ids=[call.subject_id], num_results=20)
        from neo4j import AsyncGraphDatabase
        episode_ids=list({episode for edge in edges for episode in edge.episodes})
        driver=AsyncGraphDatabase.driver(os.environ['NEO4J_URI'],auth=(os.environ['NEO4J_USER'],os.environ['NEO4J_PASSWORD']))
        try:
            records,_,_=await driver.execute_query('MATCH (e:Episodic) WHERE e.uuid IN $ids AND e.group_id=$group RETURN e.name AS canonical_id',ids=episode_ids,group=call.subject_id)
        finally:
            await driver.close()
        return {'canonical_ids':[row['canonical_id'] for row in records]}
    if name == 'purge':
        # 派生摘要可能保留删除事实，因此清空整个主体图再重建，不仅删除单条 episode。
        from neo4j import AsyncGraphDatabase
        driver = AsyncGraphDatabase.driver(os.environ['NEO4J_URI'],auth=(os.environ['NEO4J_USER'],os.environ['NEO4J_PASSWORD']))
        try:
            await driver.execute_query('MATCH (n {group_id: $group}) DETACH DELETE n', group=call.subject_id)
        finally:
            await driver.close()
        return {'purged_subject':call.subject_id,'requires_rebuild':True}
    raise HTTPException(404,'未知图索引操作')
