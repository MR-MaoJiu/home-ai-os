"""导出契约，不连接数据库、不读取实际主密钥。"""
import json
from pathlib import Path
from homeai.api import create_app
from homeai.config import Settings
from homeai.crypto import Vault
from homeai.contracts import DataRecord,TaskRequest,ProviderManifest,Skill,TaskStateNotification
app=create_app(Settings(),Vault(b'0'*32),db_factory=lambda:None)
Path('contracts/openapi.json').write_text(json.dumps(app.openapi(),ensure_ascii=False,indent=2))
for model in (DataRecord,TaskRequest,ProviderManifest,Skill,TaskStateNotification):
    Path('contracts',model.__name__+'.json').write_text(json.dumps(model.model_json_schema(),ensure_ascii=False,indent=2))
