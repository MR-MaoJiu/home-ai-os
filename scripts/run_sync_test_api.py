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
uvicorn.run(create_app(settings),host='127.0.0.1',port=58444,ssl_keyfile='state/tls/server.key',ssl_certfile='state/tls/server.crt',access_log=False)
