"""为指定成员登记已运行的本地 Reranker；服务凭据只写入加密 Secret。"""
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
    token_path=Path('state/provider-secrets/reranker.token')
    if not token_path.is_file() or token_path.is_symlink() or token_path.stat().st_mode&0o077:
        raise SystemExit('服务凭据必须为 0600 普通文件')
    token=token_path.read_text().strip()
    async with httpx.AsyncClient(trust_env=False,timeout=180) as client:
        response=await client.get('http://127.0.0.1:8109/health',headers={'Authorization':'Bearer '+token})
        response.raise_for_status()
        if response.json().get('adapter')!='reranker':raise SystemExit('端口不是 Reranker 服务')
        probe=await client.post('http://127.0.0.1:8109/invoke/rerank',headers={'Authorization':'Bearer '+token},
            json={'subject_id':args.user,'invocation_id':uid(),'arguments':{'query':'何时检查备份？',
                'documents':['周日购买水果。','星期六检查服务器备份。']}})
        probe.raise_for_status()
        from homeai.reranking import ordered_indices
        if ordered_indices(probe.json(),2)[0]!=1:raise SystemExit('真实重排探测未通过')
    vault=Vault.from_file(settings.master_key_file)
    _,factory=database(settings.database_url)
    with factory() as db:
        principal=db.get(Principal,args.user)
        if not principal:raise SystemExit('成员不存在')
        scope(db,principal.id,principal.household_id)
        provider_id='reranker.'+principal.id
        secret_id=uid()
        db.add(Secret(id=secret_id,owner_id=principal.id,household_id=principal.household_id,provider_id=provider_id,value=vault.seal(token,principal.id+':secret:'+secret_id)))
        manifest=ProviderManifest(id=provider_id,version='2cfc18c9415c912f9d8155881c133215df768a70',adapter='http',endpoint='http://127.0.0.1:8109',capabilities={'model.rerank@v1':'/invoke/rerank'},allowed_hosts=['127.0.0.1'],secret_id=secret_id,timeout_seconds=300)
        row=db.get(Provider,provider_id)
        if row:row.previous_manifest,row.manifest,row.enabled=row.manifest,manifest.model_dump_json(),True
        else:db.add(Provider(id=provider_id,manifest=manifest.model_dump_json(),enabled=True))
        db.commit()
    print('Reranker 已为指定成员登记，服务凭据不会回显。')

if __name__=='__main__':asyncio.run(main())
