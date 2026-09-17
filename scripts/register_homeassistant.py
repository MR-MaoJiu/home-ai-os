"""登记成员的 Home Assistant 令牌和明确实体清单；仅验证读取，不操作设备。"""
import argparse
from pathlib import Path
from urllib.parse import urlparse
import httpx
from homeai.config import Settings
from homeai.crypto import Vault
from homeai.db import database, Principal, Provider, Secret, scope, uid
from homeai.contracts import ProviderManifest

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--user', required=True)
parser.add_argument('--url', required=True)
parser.add_argument('--token-file', required=True, type=Path)
parser.add_argument('--entity', action='append', required=True)
parser.add_argument('--events', action='store_true', help='启用后台事件观察')
args = parser.parse_args()
settings = Settings()
if settings.environment == 'production':
    raise SystemExit('生产隔离未验收，禁止开发登记')
if args.token_file.is_symlink() or args.token_file.stat().st_mode & 0o077:
    raise SystemExit('令牌文件必须为权限 0600 的普通文件')
manifest = ProviderManifest(id='homeassistant.' + args.user, version='pending-verification', adapter='homeassistant',
    endpoint=args.url, allowed_hosts=[urlparse(args.url).hostname or ''], home_entities=args.entity, home_events=args.events,
    capabilities={'home.states@v1': 'states', 'home.execute@v1': 'execute'})
token = args.token_file.read_text().strip()
with httpx.Client(timeout=10, trust_env=False, follow_redirects=False, headers={'Authorization': 'Bearer ' + token}) as client:
    response = client.get(manifest.endpoint + '/api/config'); response.raise_for_status()
    version = response.json().get('version')
    if not isinstance(version, str) or len(version) > 100:
        raise SystemExit('服务没有返回有效版本')
    manifest.version = version
    for entity in manifest.home_entities:
        response = client.get(manifest.endpoint + '/api/states/' + entity); response.raise_for_status()
        if response.json().get('entity_id') != entity:
            raise SystemExit('实体响应与申请授权不匹配')
vault = Vault.from_file(settings.master_key_file)
_, factory = database(settings.database_url)
with factory() as db:
    user = db.get(Principal, args.user)
    if not user: raise SystemExit('Core 成员不存在')
    scope(db, user.id, user.household_id)
    secret_id = uid()
    db.add(Secret(id=secret_id, owner_id=user.id, household_id=user.household_id, provider_id=manifest.id,
        value=vault.seal(token, user.id + ':secret:' + secret_id)))
    manifest.secret_id = secret_id
    row = db.get(Provider, manifest.id)
    if row:
        row.previous_manifest, row.manifest, row.enabled = row.manifest, manifest.model_dump_json(), True
    else:
        db.add(Provider(id=manifest.id, manifest=manifest.model_dump_json(), enabled=True))
    db.commit()
print('已登记成员专属 Home Assistant 与明确实体清单；没有操作设备。')
