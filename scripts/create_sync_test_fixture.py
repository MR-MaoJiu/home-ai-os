"""在隔离测试库准备超过一页的数据，输出私有配对文件路径，不输出票据。"""
import json,tempfile
from pathlib import Path
from homeai.config import Settings
from homeai.api import create_app
from homeai.db import Principal,uid
from homeai.security import Actor,credential
from homeai.contracts import DataRecord
from homeai.data import ingest

settings=Settings();settings.database_url=settings.database_url.rsplit('/',1)[0]+'/homeai_test'
app=create_app(settings).state
with app.db() as db:
    user=Principal(id=uid(),household_id=uid(),name='原生同步验收',role='adult')
    db.add(user);db.flush()
    actor=Actor(user.id,user.household_id,'fixture','adult')
    for index in range(205):
        ingest(db,actor,DataRecord(source='native_sync',source_id=f'item-{index}',kind='note',version=1,payload={'title':f'同步测试 {index}'}),app.vault)
    token=credential(db,user.id,'pair',600)
    db.commit()
with tempfile.NamedTemporaryFile(mode='w',prefix='homeai-sync-native-',suffix='.json',delete=False) as file:
    json.dump({'pairing':{'url':'https://localhost:58444','fingerprint':Path('state/tls/fingerprint.txt').read_text().strip(),'token':token},'count':205},file)
    print(file.name)
