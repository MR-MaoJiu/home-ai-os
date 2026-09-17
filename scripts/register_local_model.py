"""注册已在本机启动的 llama.cpp；仅用于开发，不配置云模型。"""
from homeai.config import Settings
from homeai.db import database, Provider
from homeai.contracts import ProviderManifest

settings=Settings()
if settings.environment=='production':raise SystemExit('开发脚本不能用于生产')
_,factory=database(settings.database_url)
manifest=ProviderManifest(id='local.llama',version='8460',adapter='openai',endpoint='http://127.0.0.1:58080/v1',model='Qwen3-0.6B-Q8_0.gguf',capabilities={'model.generate@v1':'chat'},allowed_hosts=['127.0.0.1'])
with factory() as db:
    row=db.get(Provider,manifest.id)
    if not row:
        db.add(Provider(id=manifest.id,manifest=manifest.model_dump_json(),enabled=True,health='healthy'))
    db.commit()
print('本地开发模型已注册')
