"""登记成员绑定的本机邮件 Provider；不发送测试邮件，不把邮箱密码写入 Core。"""
import argparse
import json
from pathlib import Path
import httpx
from homeai.config import Settings
from homeai.crypto import Vault
from homeai.db import database, Principal, Provider, Secret, scope, uid
from homeai.contracts import ProviderManifest

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--config', type=Path, required=True)
parser.add_argument('--port', type=int, default=8107)
args = parser.parse_args()
settings = Settings()
if settings.environment == 'production':
    raise SystemExit('生产隔离尚未验收，禁止开发登记')
if args.config.is_symlink() or args.config.stat().st_mode & 0o077:
    raise SystemExit('配置必须为权限 0600 的普通文件')
if not 1024 <= args.port <= 65535:
    raise SystemExit('Provider 端口无效')
config = json.loads(args.config.read_text())
endpoint = f'http://127.0.0.1:{args.port}'
response = httpx.get(endpoint + '/health', headers={'Authorization': 'Bearer ' + config['PROVIDER_SERVICE_TOKEN']}, timeout=5, trust_env=False)
response.raise_for_status()
if response.json().get('adapter') != 'mail' or response.json().get('subject_id') != config['MAIL_SUBJECT_ID']:
    raise SystemExit('运行中的邮件 Provider 未绑定指定成员')
vault = Vault.from_file(settings.master_key_file)
_, factory = database(settings.database_url)
with factory() as db:
    user = db.get(Principal, config['MAIL_SUBJECT_ID'])
    if not user:
        raise SystemExit('Core 成员不存在')
    scope(db, user.id, user.household_id)
    provider_id = 'mail.' + user.id
    secret_id = uid()
    db.add(Secret(id=secret_id, owner_id=user.id, household_id=user.household_id, provider_id=provider_id,
        value=vault.seal(config['PROVIDER_SERVICE_TOKEN'], user.id + ':secret:' + secret_id)))
    manifest = ProviderManifest(id=provider_id, version='1.1.0', adapter='http', endpoint=endpoint,
        allowed_hosts=['127.0.0.1'], secret_id=secret_id, timeout_seconds=60,
        capabilities={'mail.search@v1': '/invoke/search', 'mail.read@v1': '/invoke/read', 'mail.send@v1': '/invoke/send'})
    row = db.get(Provider, provider_id)
    if row:
        row.previous_manifest, row.manifest, row.enabled = row.manifest, manifest.model_dump_json(), True
    else:
        db.add(Provider(id=provider_id, manifest=manifest.model_dump_json(), enabled=True))
    db.commit()
print('已登记成员专属邮件 Provider；每封发送仍需要审批。')
