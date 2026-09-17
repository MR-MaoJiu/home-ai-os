"""检查真实 Embedding 服务，成功后登记开发用 Provider；不提供占位向量。"""
import asyncio
import httpx
from homeai.config import Settings
from homeai.contracts import ProviderManifest
from homeai.db import database, Provider
from homeai.vector_index import vector_value


async def main():
    settings = Settings()
    if settings.environment == 'production':
        raise SystemExit('开发登记脚本禁止用于生产')
    manifest = ProviderManifest(
        id='local.embeddinggemma', version='0f741b5a6585bd53aeb15cd1372c56f2a0f65e12',
        adapter='openai', endpoint='http://127.0.0.1:58081/v1',
        model='embeddinggemma-300M-Q8_0.gguf', capabilities={'model.embed@v1': 'embed'},
        allowed_hosts=['127.0.0.1'],
        embedding_query_prefix='task: search result | query: ',
        embedding_document_prefix='title: none | text: ',
    )
    async with httpx.AsyncClient(trust_env=False, timeout=60) as client:
        response = await client.post(manifest.endpoint + '/embeddings', json={'model': manifest.model, 'input': manifest.embedding_query_prefix + '家庭资料检索'})
        response.raise_for_status()
        vector_value(response.json())
    _, factory = database(settings.database_url)
    with factory() as db:
        row = db.get(Provider, manifest.id)
        if row:
            row.previous_manifest, row.manifest = row.manifest, manifest.model_dump_json()
            row.enabled, row.health = True, 'healthy'
        else:
            db.add(Provider(id=manifest.id, manifest=manifest.model_dump_json(), enabled=True, health='healthy'))
        db.commit()
    print('真实本地 Embedding 服务检查通过，已登记；索引由 memory_worker 创建。')


if __name__ == '__main__':
    asyncio.run(main())
