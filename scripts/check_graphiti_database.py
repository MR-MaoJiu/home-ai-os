"""只验证 Graphiti 图数据库认证连接，不伪造实体抽取或语义搜索。"""
import asyncio,os,sys,argparse
from pathlib import Path
from neo4j import AsyncGraphDatabase

async def main():
    root=Path(__file__).resolve().parents[1]
    config=root/'.env.graphiti'
    if config.is_symlink() or config.stat().st_mode&0o077:raise SystemExit('配置权限无效')
    values=dict(line.split('=',1) for line in config.read_text().splitlines() if line and not line.startswith('#'))
    driver=AsyncGraphDatabase.driver('bolt://127.0.0.1:57687',auth=('neo4j',values['HOMEAI_NEO4J_PASSWORD']))
    try:
        await driver.verify_connectivity()
        rows,_,_=await driver.execute_query('RETURN 1 AS value')
        assert rows[0]['value']==1
        print('独立 Neo4j 认证连接通过；尚不代表模型抽取与检索通过。')
        if '--initialize-indexes' in sys.argv:
            os.environ.update({'GRAPHITI_TELEMETRY_ENABLED':'false','NEO4J_URI':'bolt://127.0.0.1:57687','NEO4J_USER':'neo4j','NEO4J_PASSWORD':values['HOMEAI_NEO4J_PASSWORD'],'LOCAL_MODEL_URL':'http://127.0.0.1:58082/v1','LOCAL_MODEL_NAME':'Qwen3-4B-Q4_K_M.gguf','LOCAL_EMBEDDING_URL':'http://127.0.0.1:58081/v1','LOCAL_EMBEDDING_MODEL':'embeddinggemma-300M-Q8_0.gguf','LOCAL_EMBEDDING_DIM':'768'})
            sys.path.insert(0,str(root/'providers'))
            from homeai_providers.graphiti_adapter import ensure_indexes
            client=await ensure_indexes()
            try:
                await driver.execute_query('CALL db.awaitIndexes(60)')
                indexes,_,_=await driver.execute_query('SHOW INDEXES YIELD state RETURN state')
                assert indexes and all(row['state']=='ONLINE' for row in indexes)
                print('Graphiti 索引初始化通过，在线索引数量:',len(indexes))
            finally:await client.close()
    finally:await driver.close()

if __name__=='__main__':asyncio.run(main())
