"""隔离数据库上的原生同步验收 API，不连接业务库。"""
import uvicorn
from homeai.api import create_app
from homeai.config import Settings

settings=Settings()
settings.database_url=settings.database_url.rsplit('/',1)[0]+'/homeai_test'
uvicorn.run(create_app(settings),host='127.0.0.1',port=58444,ssl_keyfile='state/tls/server.key',ssl_certfile='state/tls/server.crt',access_log=False)
