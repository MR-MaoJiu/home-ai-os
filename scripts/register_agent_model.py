"""检查真实 Agent 模型服务，成功后登记开发用 Provider；不提供模拟模型结果。"""
import asyncio
import httpx
from homeai.config import Settings
from homeai.contracts import ProviderManifest
from homeai.db import database, Provider


async def main():
    settings = Settings()
    if settings.environment == 'production':
        raise SystemExit('开发登记脚本禁止用于生产')
    manifest = ProviderManifest(
        id='local.agent4b', version='bc640142c66e1fdd12af0bd68f40445458f3869b',
        adapter='openai', endpoint='http://127.0.0.1:58082/v1',
        model='Qwen3-4B-Q4_K_M.gguf', capabilities={'model.generate@v1': 'chat'},
        allowed_hosts=['127.0.0.1'],
    )
    async with httpx.AsyncClient(trust_env=False, timeout=60) as client:
        response = await client.get(manifest.endpoint + '/models')
        response.raise_for_status()
        if not any(model.get('id') == manifest.model for model in response.json().get('data', [])):
            raise SystemExit('端口未提供预期模型')
    _, factory = database(settings.database_url)
    with factory() as db:
        row = db.get(Provider, manifest.id)
        if row:
            row.previous_manifest, row.manifest = row.manifest, manifest.model_dump_json()
            row.enabled, row.health = True, 'healthy'
        else:
            db.add(Provider(id=manifest.id, manifest=manifest.model_dump_json(), enabled=True, health='healthy'))
        db.commit()
    print('真实本地 4B 模型已登记。')


if __name__ == '__main__':
    asyncio.run(main())
