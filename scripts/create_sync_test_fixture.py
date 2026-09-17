"""在隔离测试库准备超过一页的数据，输出私有配对文件路径，不输出票据。"""
import json,tempfile,argparse
from pathlib import Path
from homeai.config import Settings
from homeai.api import create_app
from homeai.db import Principal,Provider,Secret,uid
from homeai.security import Actor,credential
from homeai.contracts import DataRecord,ProviderManifest
from homeai.data import ingest

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument("--with-documents",action="store_true")
args=parser.parse_args()
settings=Settings();settings.database_url=settings.database_url.rsplit('/',1)[0]+'/homeai_test'
app=create_app(settings).state
with app.db() as db:
    user=Principal(id=uid(),household_id=uid(),name='原生同步验收',role='adult')
    db.add(user);db.flush()
    actor=Actor(user.id,user.household_id,'fixture','adult')
    for index in range(205):
        ingest(db,actor,DataRecord(source='native_sync',source_id=f'item-{index}',kind='note',version=1,payload={'title':f'同步测试 {index}'}),app.vault)
    if args.with_documents:
        secret_id=uid();provider_id='native.docling.'+user.id
        db.add(Secret(id=secret_id,owner_id=user.id,household_id=user.household_id,provider_id=provider_id,value=app.vault.seal(Path('state/provider-secrets/docling.token').read_text().strip(),user.id+':secret:'+secret_id)))
        manifest=ProviderManifest(id=provider_id,version='2.128.0',adapter='http',endpoint='http://127.0.0.1:8103',allowed_hosts=['127.0.0.1'],secret_id=secret_id,timeout_seconds=300,capabilities={'document.parse@v1':'/invoke/parse'})
        db.add(Provider(id=provider_id,manifest=manifest.model_dump_json(),enabled=True))
        manifest=ProviderManifest(id='native.embed.'+user.id,version='1',adapter='openai',endpoint='http://127.0.0.1:58081/v1',model='embeddinggemma-300M-Q8_0.gguf',allowed_hosts=['127.0.0.1'],embedding_query_prefix='task: search result | query: ',embedding_document_prefix='title: none | text: ',capabilities={'model.embed@v1':'embed'})
        db.add(Provider(id=manifest.id,manifest=manifest.model_dump_json(),enabled=True))
    token=credential(db,user.id,'pair',600)
    db.commit()
with tempfile.NamedTemporaryFile(mode='w',prefix='homeai-sync-native-',suffix='.json',delete=False) as file:
    json.dump({'pairing':{'url':'https://localhost:58444','fingerprint':Path('state/tls/fingerprint.txt').read_text().strip(),'token':token},'count':205,'documents':args.with_documents},file)
    print(file.name)
