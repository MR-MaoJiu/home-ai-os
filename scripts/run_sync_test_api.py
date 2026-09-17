"""隔离数据库上的原生同步验收 API，不连接业务库。"""
import uvicorn
import argparse
from homeai.api import create_app
from homeai.config import Settings

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument("--session-seconds",type=int,default=900)
args=parser.parse_args()
if not 10<=args.session_seconds<=900:raise SystemExit("测试会话时长必须在 10 到 900 秒之间")
settings=Settings()
settings.session_seconds=args.session_seconds
settings.database_url=settings.database_url.rsplit('/',1)[0]+'/homeai_test'
app=create_app(settings)
from fastapi import Depends
from homeai.security import authenticate,Actor

@app.post('/_test/run/{task_id}')
async def run_fixture_task(task_id:str,actor:Actor=Depends(authenticate)):
    from homeai.runtime import run_task
    from homeai.knowledge import reconcile
    from homeai.security import own
    from homeai.db import Task
    with app.state.db() as db:own(db,Task,task_id,actor)
    await run_task(app.state,task_id,actor.user_id)
    await reconcile(app.state,actor.user_id,actor.household_id)
    with app.state.db() as db:
        task=own(db,Task,task_id,actor)
        return {'id':task.id,'status':task.status}

@app.post('/_test/pair-ticket')
def pair_ticket(actor:Actor=Depends(authenticate)):
    from homeai.security import credential
    with app.state.db() as db:
        token=credential(db,actor.user_id,'pair',300)
        db.commit()
        return {'token':token}

uvicorn.run(app,host='127.0.0.1',port=58444,ssl_keyfile='state/tls/server.key',ssl_certfile='state/tls/server.crt',access_log=False)
