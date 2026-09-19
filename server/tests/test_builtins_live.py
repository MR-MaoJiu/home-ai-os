"""真实本地权重、llama.cpp 和 Docker 验证部署器，不返回模拟模型结果。"""
import os,subprocess,sys,time
import httpx,pytest
from sqlalchemy import select,delete
from homeai.config import Settings
from homeai.db import database,BuiltinDeployment,Provider,now
pytestmark=pytest.mark.skipif(os.getenv('HOMEAI_BUILTIN_LIVE')!='1',reason='需要真实 llama.cpp、固定模型文件与 Docker')

def test_real_builtin_model_switch_and_search(tmp_path):
    settings=Settings();url=settings.database_url.rsplit('/',1)[0]+'/homeai_test'
    _,factory=database(url)
    with factory() as db:
        db.execute(delete(BuiltinDeployment));db.execute(delete(Provider).where(Provider.id.like('builtin.%')));db.commit()
    env=dict(os.environ,HOMEAI_DATABASE_URL=url,PYTHONPATH='server')
    with (tmp_path/'worker.log').open('wb') as log:
        process=subprocess.Popen([sys.executable,'scripts/run_builtin_services.py'],env=env,stdout=log,stderr=log)
    try:
        for identifier,variant in [('local-model','qwen3-0.6b'),('local-model','qwen3-4b'),('web-search','searxng')]:
            with factory() as db:
                row=db.get(BuiltinDeployment,identifier)
                if not row:row=BuiltinDeployment(id=identifier,variant=variant);db.add(row)
                row.variant=variant;row.enabled=True;row.status='queued';row.updated_at=now();db.commit()
            for _ in range(180):
                assert process.poll() is None,'部署器退出'
                with factory() as db:
                    row=db.get(BuiltinDeployment,identifier)
                    assert row.status!='failed',row.error
                    if row.status=='ready':break
                time.sleep(1)
            else:raise AssertionError('内置服务未就绪')
            if identifier=='local-model':
                r=httpx.post('http://127.0.0.1:58100/v1/chat/completions',json={'messages':[{'role':'user','content':'请只回复你好 /no_think'}],'max_tokens':32},timeout=90,trust_env=False)
                r.raise_for_status();assert r.json()['choices'][0]['message']['content']
            else:
                r=httpx.post('http://127.0.0.1:58088/search',data={'q':'Python official documentation','format':'json'},timeout=30,trust_env=False)
                r.raise_for_status();assert r.json()['results']
    finally:
        process.terminate()
        try:process.wait(timeout=20)
        except subprocess.TimeoutExpired:process.kill();process.wait()
        with factory() as db:
            for row in db.scalars(select(Provider).where(Provider.id.like('builtin.%'))):row.enabled=False
            db.execute(delete(BuiltinDeployment));db.commit()
        factory.kw['bind'].dispose()
