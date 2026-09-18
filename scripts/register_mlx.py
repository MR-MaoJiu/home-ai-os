"""真实推理探测成功后登记停用的 MLX Provider，不改变现有默认模型。"""
import json
from pathlib import Path
import httpx
from homeai.config import Settings
from homeai.contracts import ProviderManifest
from homeai.db import Provider, database

settings = Settings()
if settings.environment == 'production':
    raise SystemExit('开发登记脚本不能用于生产')
root = Path(__file__).resolve().parents[1]
manifest = ProviderManifest.model_validate(json.loads((root / 'providers/manifests/mlx.json').read_text()))
with httpx.Client(trust_env=False, timeout=60) as client:
    response = client.post(manifest.endpoint + '/chat/completions', json={
        'model': manifest.model, 'messages': [{'role': 'user', 'content': '请简短回答：你好。'}],
        'max_tokens': 32})
    response.raise_for_status()
    result = response.json()
    if not result['choices'][0]['message']['content'].strip() or result['usage']['completion_tokens'] <= 0:
        raise SystemExit('真实生成探测未通过')
_, factory = database(settings.database_url)
with factory() as db:
    row = db.get(Provider, manifest.id)
    if row:
        row.previous_manifest = row.manifest
        row.manifest = manifest.model_dump_json()
        row.enabled = False
        row.health = 'healthy'
    else:
        db.add(Provider(id=manifest.id, manifest=manifest.model_dump_json(), enabled=False, health='healthy'))
    db.commit()
print('MLX 真实生成已通过，Provider 已登记为停用。请在管理后台按需启用。')
