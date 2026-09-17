import os
from datetime import datetime, timezone
from functools import lru_cache
from urllib.parse import urlparse
from fastapi import HTTPException


@lru_cache
def graph():
    from graphiti_core import Graphiti
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
    from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
    from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
    base = os.environ['LOCAL_MODEL_URL']
    embed_base = os.environ['LOCAL_EMBEDDING_URL']
    allowed = os.environ.get('LOCAL_MODEL_HOSTS', '127.0.0.1,localhost').split(',')
    if any(urlparse(url).hostname not in allowed for url in (base, embed_base)):
        raise RuntimeError('Graphiti 只能调用许可的本地模型')
    config = LLMConfig(api_key='local-only', model=os.environ['LOCAL_MODEL_NAME'], small_model=os.environ['LOCAL_MODEL_NAME'], base_url=base)
    return Graphiti(os.environ['NEO4J_URI'], os.environ['NEO4J_USER'], os.environ['NEO4J_PASSWORD'],
        llm_client=OpenAIGenericClient(config=config),
        embedder=OpenAIEmbedder(OpenAIEmbedderConfig(api_key='local-only',base_url=embed_base,embedding_model=os.environ['LOCAL_EMBEDDING_MODEL'],embedding_dim=int(os.environ['LOCAL_EMBEDDING_DIM']))),
        cross_encoder=OpenAIRerankerClient(config=config))


async def operation(name, call):
    client = graph()
    if name == 'index':
        from graphiti_core.nodes import EpisodeType
        await client.build_indices_and_constraints()
        episode = await client.add_episode(name=call.arguments['record_id'], episode_body=call.arguments['content'], source=EpisodeType.text, source_description='Home AI canonical memory', reference_time=datetime.now(timezone.utc), group_id=call.subject_id)
        return {'episode_id': episode.episode.uuid, 'canonical_id': call.arguments['record_id']}
    if name == 'search':
        edges = await client.search(call.arguments['query'], group_ids=[call.subject_id], num_results=20)
        return {'edges': [edge.model_dump(mode='json') for edge in edges]}
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
