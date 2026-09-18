"""真实本地视觉模型与照片账本权限验收，不替换模型返回值。"""
import base64
import os
import uuid
from pathlib import Path
import httpx
import pytest
from conftest import SignedClient
from test_workflows import workflow
from test_security_data import put
from homeai.contracts import ProviderManifest
from homeai.db import Principal,Provider,Secret,scope,uid
from homeai.runtime import run_task

pytestmark=pytest.mark.skipif(os.getenv('HOMEAI_VISION_TEST')!='1',reason='需要真实视觉模型、桥接服务与公开照片样本')


def test_bridge_rejects_remote_urls_and_invalid_images():
    token=Path('state/provider-secrets/vision.token').read_text().strip()
    with httpx.Client(trust_env=False,timeout=20) as client:
        assert client.get('http://127.0.0.1:8110/health').status_code==401
        for arguments in [{'question':'描述图片','image_url':'http://127.0.0.1/private'},
                          {'question':'描述图片','content_base64':base64.b64encode(b'not an image').decode()}]:
            r=client.post('http://127.0.0.1:8110/invoke/analyze',headers={'Authorization':'Bearer '+token},
                json={'subject_id':'vision-test','invocation_id':str(uuid.uuid4()),'arguments':arguments})
            assert r.status_code==422,r.text


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv('HOMEAI_INTEGRATION')!='1',reason='需要真实 PostgreSQL/OPA')
async def test_real_photo_task_and_revocation(workflow):
    app,user=workflow
    content=base64.b64encode(Path('state/vision/official-sample.jpg').read_bytes()).decode()
    provider_id='a-vision.'+uuid.uuid4().hex
    with app.db() as db:
        principal=db.get(Principal,user.user_id);household=principal.household_id;scope(db,user.user_id,household)
        secret_id=uid();token=Path('state/provider-secrets/vision.token').read_text().strip()
        db.add(Secret(id=secret_id,owner_id=user.user_id,household_id=household,provider_id=provider_id,
            value=app.vault.seal(token,user.user_id+':secret:'+secret_id)))
        manifest=ProviderManifest(id=provider_id,version='bb307c036e8a1ed7b663bbd0c35b41c4c9294cfd',adapter='http',
            endpoint='http://127.0.0.1:8110',allowed_hosts=['127.0.0.1'],secret_id=secret_id,timeout_seconds=180,
            capabilities={'photo.analyze@v1':'/invoke/analyze'})
        db.add(Provider(id=provider_id,manifest=manifest.model_dump_json(),enabled=True));db.commit()
    async def execute(actor,arguments):
        r=actor.request('POST','/api/v1/tasks',{'capability':'photo.analyze@v1','arguments':arguments,
            'step_timeout_seconds':180,'idempotency_key':str(uuid.uuid4())})
        assert r.status_code==202,r.text
        tid=r.json()['id'];await run_task(app,tid,actor.user_id)
        return tid,actor.request('GET','/api/v1/tasks/'+tid).json()
    try:
        rid=put(user,{'source':'photos','source_id':str(uuid.uuid4()),'kind':'photo.selected','version':1,
            'payload':{'name':'官方公开山谷样本','content_base64':content}})
        tid,result=await execute(user,{'record_id':rid,'question':'用中文描述这张照片的自然景物。'})
        assert result['status']=='SUCCEEDED',result
        text=result['result']['text'];assert '山' in text and any(word in text for word in ('河','湖','水')),text
        assert result['result']['source_id']==rid and result['result']['source_version']==1
        other=SignedClient(user.client,app.db,household=household)
        _,denied=await execute(other,{'record_id':rid})
        assert denied['status']=='FAILED' and '未授权' in denied['error'],denied
        _,invalid=await execute(user,{'messages':[{'role':'user','content':'arbitrary URL'}]})
        assert invalid['status']=='FAILED' and 'record_id' in invalid['error'],invalid
        secret=put(user,{'source':'photos','source_id':str(uuid.uuid4()),'kind':'photo.selected','version':1,
            'sensitivity':'SECRET','cloud_policy':'LOCAL_ONLY','payload':{'content_base64':content}})
        _,denied=await execute(user,{'record_id':secret})
        assert denied['status']=='FAILED' and '秘密' in denied['error'],denied
        assert user.request('DELETE','/api/v1/data/'+rid).status_code==200
        cached=user.request('GET','/api/v1/tasks/'+tid).json()
        assert cached['result'] is None and cached['result_redacted'] is True,cached
    finally:
        with app.db() as db:db.get(Provider,provider_id).enabled=False;db.commit()
