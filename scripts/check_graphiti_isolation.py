"""用独立测试节点验证 Graphiti 主体清理隔离，不模拟模型抽取。"""
import asyncio,uuid
from pathlib import Path
import httpx
from neo4j import AsyncGraphDatabase

async def main():
    root=Path(__file__).resolve().parents[1]
    config=root/'.env.graphiti'
    if config.is_symlink() or config.stat().st_mode&0o077:raise SystemExit('配置权限无效')
    values=dict(line.split('=',1) for line in config.read_text().splitlines() if line and not line.startswith('#'))
    driver=AsyncGraphDatabase.driver('bolt://127.0.0.1:57687',auth=('neo4j',values['HOMEAI_NEO4J_PASSWORD']))
    run=uuid.uuid4().hex;first='validation_'+uuid.uuid4().hex;second='validation_'+uuid.uuid4().hex
    headers={'Authorization':'Bearer '+(root/'state/provider-secrets/graphiti.token').read_text().strip()}
    try:
        await driver.execute_query('CREATE (:ProviderValidation {run:$run,group_id:$first}),(:ProviderValidation {run:$run,group_id:$second})',run=run,first=first,second=second)
        async with httpx.AsyncClient(trust_env=False,headers=headers,timeout=60) as client:
            result=await client.post('http://127.0.0.1:8102/invoke/purge',json={'subject_id':first,'invocation_id':str(uuid.uuid4()),'arguments':{}})
            result.raise_for_status()
            assert result.json()['purged_subject']==first
            rows,_,_=await driver.execute_query('MATCH (n:ProviderValidation {run:$run}) RETURN n.group_id AS group_id',run=run)
            assert [row['group_id'] for row in rows]==[second]
            health=(await client.get('http://127.0.0.1:8102/health')).json()
            assert health['egress']['allowed_connections']>0 and health['egress']['blocked_connections']==0
        print('真实 Neo4j 主体清理隔离通过；未执行模型抽取或图检索。')
    finally:
        await driver.execute_query('MATCH (n:ProviderValidation {run:$run}) DETACH DELETE n',run=run)
        await driver.close()

if __name__=='__main__':asyncio.run(main())
