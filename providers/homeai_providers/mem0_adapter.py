"""固定版本、本地模型和本地向量存储的 Mem0 投影。"""
import json
import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse
from importlib.metadata import version
from fastapi import HTTPException


@lru_cache
def engine():
    if os.environ.get('MEM0_TELEMETRY', '').lower() != 'false':
        raise RuntimeError('必须显式关闭 Mem0 遥测')
    if version('mem0ai') != '1.0.11':
        raise RuntimeError('Mem0 版本不符')
    config=json.loads(Path(os.environ['MEM0_CONFIG_FILE']).read_text())
    if set(config) != {'llm','embedder','vector_store','history_db_path'}:
        raise RuntimeError('Mem0 配置必须完整且不能启用额外云能力')
    for key in ('llm','embedder'):
        component=config[key]
        endpoint=component.get('config',{}).get('lmstudio_base_url','')
        url=urlparse(endpoint)
        if component.get('provider')!='lmstudio' or url.scheme!='http' or url.hostname not in {'127.0.0.1','::1'} or url.username or url.password or url.query or url.fragment:
            raise RuntimeError('Mem0 仅允许显式回环地址的兼容模型接口')
    vector=config['vector_store']
    if vector.get('provider')!='qdrant' or set(vector['config'])-{'path','collection_name','embedding_model_dims','on_disk'}:
        raise RuntimeError('Mem0 必须使用本地 Qdrant')
    if not vector['config'].get('path') or not vector['config'].get('embedding_model_dims') or not vector['config'].get('collection_name'):
        raise RuntimeError('向量存储路径、维度和集合必须显式配置')
    if config['history_db_path']!=':memory:':
        raise RuntimeError('派生历史不得持久化私人事实副本')
    from mem0 import Memory
    from openai import OpenAI
    import httpx
    memory = Memory.from_config(config)
    # SDK 默认客户端会读取系统代理；本地私密流量不能经过宿主代理。
    for component, key in ((memory.llm, 'llm'), (memory.embedding_model, 'embedder')):
        component.client.close()
        component.client = OpenAI(base_url=config[key]['config']['lmstudio_base_url'], api_key=config[key]['config'].get('api_key','local-only'), http_client=httpx.Client(trust_env=False, timeout=60))
    return memory


def operation(name, call):
    memory=engine()
    if name=='index':
        return memory.add(os.environ.get('MEM0_EMBED_DOCUMENT_PREFIX','')+str(call.arguments['content']), user_id=call.subject_id, metadata={'canonical_id':call.arguments['record_id']}, infer=False)
    if name=='search':
        result=memory.search(os.environ.get('MEM0_EMBED_QUERY_PREFIX','')+str(call.arguments['query']),user_id=call.subject_id,limit=20)
        return {'canonical_ids':[row['metadata']['canonical_id'] for row in result.get('results',[]) if row.get('metadata',{}).get('canonical_id')]}
    if name=='purge':
        # SDK delete_all 默认仅列举 100 条；循环确认主体已清空，不能丢失后续页。
        for _ in range(10000):
            rows=memory.vector_store.list(filters={'user_id':call.subject_id},limit=1)[0]
            if not rows:
                # 固定版 reset() 嵌套获取非重入锁；只清空内存历史，不调用该路径。
                with memory.db._lock:
                    memory.db.connection.execute('DELETE FROM history')
                    memory.db.connection.commit()
                return {'purged_subject':call.subject_id,'requires_rebuild':True}
            memory.delete_all(user_id=call.subject_id)
        raise RuntimeError('派生清理超过限制，不能报告完成')
    raise HTTPException(404,'未知 Mem0 操作')
