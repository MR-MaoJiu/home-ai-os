"""为指定成员登记已运行的本地 Mem0；服务凭据只写入加密 Secret。"""
import argparse
import asyncio
from pathlib import Path
import httpx
from homeai.config import Settings
from homeai.crypto import Vault
from homeai.db import database, Principal, Provider, Secret, scope, uid
from homeai.contracts import ProviderManifest

async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user', required=True)
    args=parser.parse_args()
    settings=Settings()
    if settings.environment=='production':raise SystemExit('生产沙箱尚未验收，禁止开发登记')
    token=Path('state/provider-secrets/mem0.token').read_text().strip()
    async with httpx.AsyncClient(trust_env=False,timeout=10) as client:
        response=await client.get('http://127.0.0.1:8101/health',headers={'Authorization':'Bearer '+token})
        response.raise_for_status()
        if response.json().get('adapter')!='mem0':raise SystemExit('端口不是 Mem0 服务')
    vault=Vault.from_file(settings.master_key_file)
    _,factory=database(settings.database_url)
    with factory() as db:
        principal=db.get(Principal,args.user)
        if not principal:raise SystemExit('成员不存在')
        scope(db,principal.id,principal.household_id)
        provider_id='mem0.'+principal.id
        secret_id=uid()
        db.add(Secret(id=secret_id,owner_id=principal.id,household_id=principal.household_id,provider_id=provider_id,value=vault.seal(token,principal.id+':secret:'+secret_id)))
        manifest=ProviderManifest(id=provider_id,version='1.0.11',adapter='http',endpoint='http://127.0.0.1:8101',capabilities={'memory.semantic.search@v1':'/invoke/search','memory.semantic.index@v1':'/invoke/index','memory.semantic.purge@v1':'/invoke/purge'},allowed_hosts=['127.0.0.1'],secret_id=secret_id,timeout_seconds=300)
        row=db.get(Provider,provider_id)
        if row:row.previous_manifest,row.manifest,row.enabled=row.manifest,manifest.model_dump_json(),True
        else:db.add(Provider(id=provider_id,manifest=manifest.model_dump_json(),enabled=True))
        db.commit()
    print('Mem0 已为指定成员登记，服务凭据不会回显。')

if __name__=='__main__':asyncio.run(main())
