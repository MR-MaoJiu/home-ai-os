"""登记经过版本核查的本机搜索 Provider；公开搜索由服务端自动执行。"""
import json
from pathlib import Path
import httpx
from homeai.config import Settings
from homeai.contracts import ProviderManifest
from homeai.db import database, Provider

settings = Settings()
if settings.environment == 'production':
    raise SystemExit('生产隔离尚未验收，禁止开发登记')
manifest = ProviderManifest.model_validate_json(Path('providers/manifests/searxng.json').read_text())
response = httpx.get(manifest.endpoint + '/config', timeout=10, trust_env=False)
response.raise_for_status()
version = response.json().get('version', '')
if '274b63b67' not in version:
    raise SystemExit('搜索实例版本与固定清单不匹配')
_, factory = database(settings.database_url)
with factory() as db:
    row = db.get(Provider, manifest.id)
    if row:
        row.previous_manifest, row.manifest, row.enabled, row.health = row.manifest, manifest.model_dump_json(), True, 'ready'
    else:
        db.add(Provider(id=manifest.id, manifest=manifest.model_dump_json(), enabled=True, health='ready'))
    db.commit()
print('已登记本机 SearXNG；公开查询会发往上游引擎，私人内容仍受出站策略限制。')
