"""图片字节、加密账本和权限使用真实实现；仅模型 HTTP 与概率检测用隔离替身。"""
import base64
import json
import uuid
from email import policy
from email.parser import BytesParser

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func

from conftest import SignedClient
from test_media import png, upload
from homeai import image_generation, cloud_gateway
from homeai.cloud_gateway import CloudPermit
from homeai.contracts import ProviderManifest, TaskRequest
from homeai.crypto import canonical, digest
from homeai.db import Disclosure, Provider, Record, now, scope
from homeai.image_generation import ImageUsage
from homeai.media import MediaJob, MediaUpload, directory, probe as inspect_media
from homeai.model_routing import ModelUsage, BudgetExceeded, fingerprint
from homeai.providers import Registry
from homeai.runtime import submit
from homeai.security import Actor


def setup_images(system, alice, *, cloud=True, image_limit=10, token_limit=100000, model='gpt-image-1'):
    app=system[0].state
    manifest=ProviderManifest(id='cloud.images' if cloud else 'local.images',version='1',adapter='openai',
        endpoint='https://images.example.invalid/v1' if cloud else 'http://127.0.0.1:58911/v1',
        model=model,cloud=cloud,allowed_hosts=['images.example.invalid' if cloud else '127.0.0.1'],
        capabilities={'image.generate@v1':'generations','image.edit@v1':'edits','model.generate@v1':'chat'})
    with system[2]() as db:
        db.add(Provider(id=manifest.id,manifest=manifest.model_dump_json(),enabled=True));db.commit()
    response=alice.request('PUT','/api/v1/manage/models',{
        'image_generation':manifest.id,'image_editing':manifest.id,
        'household_daily_images':image_limit,'member_daily_images':image_limit,
        'household_daily_tokens':token_limit,'member_daily_tokens':token_limit})
    assert response.status_code==200,response.text
    return app,manifest,Actor(alice.user_id,'h1',alice.device_id,'infrastructure_owner')


def make_task(app,db,actor,message='画一个蓝色方块'):
    scope(db,actor.user_id,actor.household_id)
    task=submit(db,actor,TaskRequest(message=message,idempotency_key=str(uuid.uuid4())),app.vault)
    db.commit()
    return task,app.vault.open(task.request,actor.user_id+':task:'+task.id)


def image_response(raw=None):
    return {'data':[{'b64_json':base64.b64encode(raw if raw is not None else png()).decode()}]}


def arguments(*,with_image=False):
    content=[{'type':'text','text':'Draw a blue square.'}]
    if with_image:
        frame=inspect_media(png(),'image',frames=True,cloud=True)['frames'][0]['content_base64']
        content.append({'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+frame}})
    return {'messages':[{'role':'user','content':content}],'image_options':{'size':'1024x1024'},'max_tokens':1}


async def allow_privacy(*args,review=False):
    return {'safe':True,'uncertain':False} if review else {'entities':[],'sensitive':False,'uncertain':False}


def multipart_fields(request):
    message=BytesParser(policy=policy.default).parsebytes(
        b'Content-Type: '+request.headers['content-type'].encode()+b'\r\nMIME-Version: 1.0\r\n\r\n'+request.content)
    assert message.is_multipart()
    return {part.get_param('name',header='content-disposition'):part for part in message.iter_parts()}


@pytest.mark.asyncio
@pytest.mark.parametrize('model,expects_format',[('gpt-image-1',False),('compatible-image-model',True)])
async def test_actual_json_and_multipart_requests(system,alice,model,expects_format):
    app,manifest,actor=setup_images(system,alice,cloud=False,model=model)
    seen=[]
    def transport(request):
        seen.append(request)
        return httpx.Response(200,json=image_response())
    app.registry=Registry(app.vault,httpx.MockTransport(transport))
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        await app.registry.invoke(db,actor,manifest,'image.generate@v1',arguments(),'generate-contract')
        edited=arguments(with_image=True)
        await app.registry.invoke(db,actor,manifest,'image.edit@v1',edited,'edit-contract')
    assert [request.url.path for request in seen]==['/v1/images/generations','/v1/images/edits']
    body=json.loads(seen[0].content)
    assert body['model']==model and body['prompt']=='Draw a blue square.' and body['n']==1 and body['size']=='1024x1024'
    assert 'messages' not in body and 'image' not in body
    assert ('response_format' in body)==expects_format
    assert seen[0].headers['Idempotency-Key']=='generate-contract'
    fields=multipart_fields(seen[1])
    assert fields['model'].get_payload(decode=True).decode()==model
    assert fields['prompt'].get_payload(decode=True)==b'Draw a blue square.'
    assert fields['n'].get_payload(decode=True)==b'1'
    assert fields['image'].get_content_type()=='image/jpeg'
    assert fields['image'].get_filename()=='source.jpg'
    assert fields['image'].get_payload(decode=True)==base64.b64decode(edited['messages'][0]['content'][1]['image_url']['url'].split(',',1)[1])
    assert ('response_format' in fields)==expects_format


@pytest.mark.asyncio
@pytest.mark.parametrize('capability,with_image',[('image.generate@v1',True),('image.edit@v1',False)])
async def test_wrong_image_shape_never_reaches_transport(system,alice,capability,with_image):
    app,manifest,actor=setup_images(system,alice,cloud=False)
    app.registry=Registry(app.vault,httpx.MockTransport(lambda request:pytest.fail('错误接口参数不得出站')))
    with system[2]() as db:
        with pytest.raises(HTTPException) as denied:
            await app.registry.invoke(db,actor,manifest,capability,arguments(with_image=with_image),'invalid-shape')
        assert denied.value.status_code==422


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['missing','expired','other_owner','other_provider','changed_arguments'])
async def test_cloud_images_require_current_bound_permit(system,alice,invalid):
    app,manifest,actor=setup_images(system,alice)
    app.registry=Registry(app.vault,httpx.MockTransport(lambda request:pytest.fail('云图片许可无效不得出站')))
    args=arguments()
    permit=None if invalid=='missing' else CloudPermit(
        'foreign-owner' if invalid=='other_owner' else actor.user_id,
        'wrong-fingerprint' if invalid=='other_provider' else fingerprint(manifest),
        digest(canonical(args)),now()-1 if invalid=='expired' else now()+60,'image.generate@v1')
    if invalid=='changed_arguments':args['image_options']['size']='auto'
    with system[2]() as db:
        with pytest.raises(HTTPException) as denied:
            await app.registry.invoke(db,actor,manifest,'image.generate@v1',args,'invalid-permit',cloud_permit=permit)
        assert denied.value.status_code==403


@pytest.mark.asyncio
async def test_chat_permit_cannot_be_reused_for_billable_image_generation(system,alice):
    app,manifest,actor=setup_images(system,alice)
    seen=[]
    app.registry=Registry(app.vault,httpx.MockTransport(lambda request:(seen.append(request),httpx.Response(200,json=image_response()))[1]))
    args=arguments()
    # 默认许可是聊天能力，不能换成同 Provider 的收费图片接口。
    permit=CloudPermit(actor.user_id,fingerprint(manifest),digest(canonical(args)),now()+60)
    with system[2]() as db:
        with pytest.raises(HTTPException) as denied:
            await app.registry.invoke(db,actor,manifest,'image.generate@v1',args,'swapped-capability',cloud_permit=permit)
        assert denied.value.status_code==403
    assert seen==[]


@pytest.mark.asyncio
async def test_generation_decodes_and_stores_real_encrypted_asset_with_isolation(system,alice):
    app,manifest,actor=setup_images(system,alice,cloud=False)
    raw=png()
    app.registry=Registry(app.vault,httpx.MockTransport(lambda request:httpx.Response(200,json=image_response(raw))))
    with system[2]() as db:
        task,body=make_task(app,db,actor)
        result=await image_generation.execute(app,db,actor,task,body,'image.generate@v1',{'prompt':'画蓝色方块'},'local-image-1')
        db.commit()
        part=result['parts'][0];rid=part['record_id']
        assert part['type']=='image' and part['version']==1 and part['provenance']['origin']=='generated'
        assert part['provenance']['model']==manifest.model
        record=db.get(Record,rid)
        assert record.owner_id==alice.user_id and record.kind=='photo.file' and record.visibility=='personal'
        assert record.sensitivity=='SENSITIVE' and record.cloud_policy=='LOCAL_ONLY'
        row=db.scalar(select(MediaUpload).where(MediaUpload.record_id==rid))
        assert row.status=='completed' and row.sha256==digest(raw)
        assert db.scalar(select(MediaJob).where(MediaJob.record_id==rid)).status=='queued'
        folder=directory(app,row.id)
        sealed=(folder/'0.enc').read_text()
        assert raw not in (folder/'0.enc').read_bytes() and base64.b64encode(raw).decode() not in sealed
        assert base64.b64decode(app.vault.open(sealed,alice.user_id+':media:'+row.id+':0'))==raw
        assert (folder/'0.enc').stat().st_mode & 0o777==0o600
        assert db.scalar(select(ImageUsage)).status=='SUCCEEDED'
    metadata=alice.request('GET','/api/v1/assets/'+rid)
    assert metadata.status_code==200,metadata.text
    assert metadata.json()['sha256']==digest(raw)
    assert alice.request('GET','/api/v1/assets/'+rid+'/content').content==raw
    piece=alice.request('GET','/api/v1/assets/'+rid+'/content?offset=2&length=7')
    assert piece.status_code==206 and piece.content==raw[2:9]
    assert piece.headers['cache-control']=='no-store'
    assert alice.request('GET','/api/v1/assets/'+rid+'/thumbnail').content.startswith(b'\xff\xd8\xff')
    for other in (SignedClient(system[1],system[2]),SignedClient(system[1],system[2],household='h2')):
        for suffix in ('','/content','/thumbnail'):
            assert other.request('GET','/api/v1/assets/'+rid+suffix).status_code==404
    assert alice.request('DELETE','/api/v1/data/'+rid).status_code==200
    assert alice.request('GET','/api/v1/assets/'+rid+'/content').status_code==404


@pytest.mark.asyncio
@pytest.mark.parametrize('value,status',[
    ({'data':[]},502),({'data':[{},{}]},502),({'data':[{'b64_json':'!!!!'}]},502),
    ({'data':[{'b64_json':''}]},413),({'data':[{'b64_json':base64.b64encode(b'12345').decode()}]},413),
])
async def test_generated_image_response_rejects_invalid_or_oversized_bytes(value,status):
    with pytest.raises(HTTPException) as error:await image_generation.image_bytes(value,4)
    assert error.value.status_code==status


@pytest.mark.asyncio
async def test_invalid_decoded_image_is_not_published_as_asset(system,alice):
    app,manifest,actor=setup_images(system,alice,cloud=False)
    app.registry=Registry(app.vault,httpx.MockTransport(lambda request:httpx.Response(200,json=image_response(b'not a picture'))))
    with system[2]() as db:
        task,body=make_task(app,db,actor)
        with pytest.raises(HTTPException) as error:
            await image_generation.execute(app,db,actor,task,body,'image.generate@v1',{'prompt':'公开描述'},'invalid-output')
        assert error.value.status_code==502
        db.commit()
        assert db.scalar(select(func.count()).select_from(MediaUpload))==0
        assert db.scalar(select(func.count()).select_from(Record))==0
        assert db.scalar(select(ImageUsage.charged))==1


@pytest.mark.asyncio
@pytest.mark.parametrize('exhausted',['images','tokens'])
async def test_gateway_two_budget_reservations_are_atomic(system,alice,monkeypatch,exhausted):
    app,manifest,actor=setup_images(system,alice,image_limit=1,token_limit=100000 if exhausted=='images' else 1)
    monkeypatch.setattr(cloud_gateway,'local_check',allow_privacy)
    app.registry=Registry(app.vault,httpx.MockTransport(lambda request:pytest.fail('额度不足不得出站')))
    with system[2]() as db:
        task,body=make_task(app,db,actor)
        if exhausted=='images':
            previous=image_generation.reserve_images(app,db,actor,task.id,'previous-call');previous.status='UNKNOWN';db.commit()
        with pytest.raises(BudgetExceeded):
            await cloud_gateway.invoke(app,db,actor,manifest,task,body,arguments(),'blocked-call',capability='image.generate@v1')
        # 即使调用者保存其他状态，失败的嵌套事务也不能留下另一份预算预留。
        db.commit()
        assert db.scalar(select(func.count()).select_from(ModelUsage))==0
        assert db.scalar(select(func.count()).select_from(ImageUsage))==(1 if exhausted=='images' else 0)
        assert db.scalar(select(func.count()).select_from(Disclosure))==0
        assert '_cloud_calls' not in body and '_image_dispatched' not in body


@pytest.mark.asyncio
async def test_timeout_keeps_both_charges_and_same_key_cannot_send_twice(system,alice,monkeypatch):
    app,manifest,actor=setup_images(system,alice,image_limit=1)
    monkeypatch.setattr(cloud_gateway,'local_check',allow_privacy)
    seen=[]
    def transport(request):
        seen.append(request)
        raise httpx.ReadTimeout('测试响应未知',request=request)
    app.registry=Registry(app.vault,httpx.MockTransport(transport))
    with system[2]() as db:
        task,body=make_task(app,db,actor)
        with pytest.raises(HTTPException) as error:
            await cloud_gateway.invoke(app,db,actor,manifest,task,body,arguments(),'uncertain-call',capability='image.generate@v1')
        assert error.value.status_code==504
        assert db.scalar(select(ImageUsage)).status=='UNKNOWN'
        assert db.scalar(select(ImageUsage)).charged==1
        assert db.scalar(select(ModelUsage)).status=='UNKNOWN'
        assert db.scalar(select(ModelUsage)).charged>0
        assert body['_cloud_calls']==1 and body['_image_dispatched']=='uncertain-call'
        with pytest.raises(HTTPException) as replay:
            await cloud_gateway.invoke(app,db,actor,manifest,task,body,arguments(),'uncertain-call',capability='image.generate@v1')
        assert replay.value.status_code==409 and not isinstance(replay.value,BudgetExceeded)
        db.commit()
        assert db.scalar(select(func.count()).select_from(ImageUsage))==1
        assert db.scalar(select(func.count()).select_from(ModelUsage))==1
    assert len(seen)==1


@pytest.mark.asyncio
async def test_public_probe_separates_generation_from_edit_and_does_not_publish_assets(system,alice):
    app,manifest,actor=setup_images(system,alice)
    seen=[]
    def transport(request):
        seen.append(request)
        return httpx.Response(200,json=image_response())
    app.registry=Registry(app.vault,httpx.MockTransport(transport))
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        result=await image_generation.probe(app,db,actor,manifest,generate=True,edit=True)
        assert result['image_generation'] and result['image_editing'],result
        assert db.scalar(select(func.sum(ImageUsage.charged)))==2
        assert set(db.scalars(select(ImageUsage.status)))=={'SUCCEEDED'}
        assert db.scalar(select(func.count()).select_from(MediaUpload))==0
    assert [request.url.path for request in seen]==['/v1/images/generations','/v1/images/edits']
    assert json.loads(seen[0].content)['prompt']=='Create a plain blue square on a white background.'
    assert 'image' not in json.loads(seen[0].content)
    assert multipart_fields(seen[1])['image'].get_payload(decode=True).startswith(b'\xff\xd8\xff')


@pytest.mark.asyncio
async def test_probe_retains_first_success_when_second_image_quota_is_exhausted(system,alice):
    app,manifest,actor=setup_images(system,alice,image_limit=1)
    seen=[]
    app.registry=Registry(app.vault,httpx.MockTransport(lambda request:(seen.append(request),httpx.Response(200,json=image_response()))[1]))
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        report=await image_generation.probe(app,db,actor,manifest,generate=True,edit=True)
        assert report['image_generation'] and not report['image_editing']
        assert report['errors']['image_editing'] and report['status']=='verified'
        assert db.scalar(select(func.sum(ImageUsage.charged)))==1
    assert len(seen)==1 and seen[0].url.path=='/v1/images/generations'


@pytest.mark.asyncio
async def test_policy_denial_does_not_charge_a_probe_that_was_never_sent(system,alice):
    from homeai.policy import Policy
    app,manifest,actor=setup_images(system,alice)
    app.policy=Policy('http://opa',httpx.MockTransport(lambda request:httpx.Response(200,json={'result':False})))
    app.registry=Registry(app.vault,httpx.MockTransport(lambda request:pytest.fail('策略拒绝不得出站')))
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        report=await image_generation.probe(app,db,actor,manifest,generate=True,edit=False)
        assert not report['image_generation'] and report['errors']['image_generation']
        assert db.scalar(select(func.count()).select_from(ImageUsage))==0


@pytest.mark.asyncio
async def test_edit_requires_separate_cloud_consent_before_any_billable_dispatch(system,alice,monkeypatch):
    app,manifest,actor=setup_images(system,alice)
    monkeypatch.setattr(cloud_gateway,'local_check',allow_privacy)
    app.registry=Registry(app.vault,httpx.MockTransport(lambda request:pytest.fail('没有原图披露同意不得发出编辑请求')))
    source=upload(alice,png(),'本人原图.png','image').json()['asset']['record_id']
    with system[2]() as db:
        task,body=make_task(app,db,actor,message='请编辑原图为蓝色方块')
        with pytest.raises(cloud_gateway.CloudConsentRequired) as required:
            await image_generation.execute(app,db,actor,task,body,'image.edit@v1',{'prompt':'换成蓝色','record_id':source},'edit-without-consent')
        assert required.value.record_versions=={source:1}
        assert db.scalar(select(func.count()).select_from(ImageUsage))==0
        assert db.scalar(select(func.count()).select_from(ModelUsage))==0


@pytest.mark.asyncio
async def test_cloud_generation_reserves_and_settles_both_ledgers_then_publishes_asset(system,alice,monkeypatch):
    app,manifest,actor=setup_images(system,alice)
    monkeypatch.setattr(cloud_gateway,'local_check',allow_privacy)
    seen=[]
    def transport(request):
        seen.append(request)
        return httpx.Response(200,json={**image_response(),'usage':{'total_tokens':11}})
    app.registry=Registry(app.vault,httpx.MockTransport(transport))
    with system[2]() as db:
        task,body=make_task(app,db,actor)
        result=await image_generation.execute(app,db,actor,task,body,'image.generate@v1',{'prompt':'画一个蓝色方块'},'cloud-success')
        db.commit()
        rid=result['parts'][0]['record_id']
        assert db.scalar(select(ImageUsage)).status=='SUCCEEDED'
        assert db.scalar(select(ImageUsage)).charged==1
        assert db.scalar(select(ModelUsage)).status=='SUCCEEDED'
        assert db.scalar(select(ModelUsage)).charged==11
        assert db.scalar(select(Disclosure)).status=='SUCCEEDED'
        assert body['_cloud_calls']==1 and body['_image_dispatched']=='cloud-success'
    assert len(seen)==1 and seen[0].url.path=='/v1/images/generations'
    assert alice.request('GET','/api/v1/assets/'+rid+'/content').content==png()


@pytest.mark.asyncio
async def test_authorized_cloud_edit_preserves_source_version_and_real_multipart(system,alice,monkeypatch):
    from homeai.client_actions import create_request
    from homeai.db import Task
    app,manifest,actor=setup_images(system,alice)
    monkeypatch.setattr(cloud_gateway,'local_check',allow_privacy)
    seen=[]
    def transport(request):
        seen.append(request)
        return httpx.Response(200,json=image_response())
    app.registry=Registry(app.vault,httpx.MockTransport(transport))
    source=upload(alice,png(),'原图.png','image').json()['asset']['record_id']
    with system[2]() as db:
        task,body=make_task(app,db,actor,message='请编辑原图为蓝色方块')
        with pytest.raises(cloud_gateway.CloudConsentRequired) as required:
            await image_generation.execute(app,db,actor,task,body,'image.edit@v1',{'prompt':'换成蓝色','record_id':source},'approved-edit')
        assert seen==[]
        consent=required.value
        task.request=app.vault.seal(body,actor.user_id+':task:'+task.id)
        request=create_request(app,db,actor,task,'cloud.disclose',consent.detail,{
            'scope_hash':consent.scope_hash,'record_versions':consent.record_versions,'provider_id':manifest.id})
        task_id=task.id;db.commit()
    assert alice.request('POST','/api/v1/client-actions/'+request['client_action_id']+'/respond',{'idempotency_key':'approve-edit-once'}).status_code==200
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,task_id)
        body=app.vault.open(task.request,alice.user_id+':task:'+task_id)
        result=await image_generation.execute(app,db,actor,task,body,'image.edit@v1',{'prompt':'换成蓝色','record_id':source},'approved-edit')
        db.commit()
        part=result['parts'][0]
        assert part['provenance']['origin']=='edited' and part['provenance']['source_versions']=={source:1}
        assert db.scalar(select(func.sum(ImageUsage.charged)))==1
    assert len(seen)==1 and seen[0].url.path=='/v1/images/edits'
    assert multipart_fields(seen[0])['image'].get_payload(decode=True).startswith(b'\xff\xd8\xff')
    assert alice.request('GET','/api/v1/assets/'+part['record_id']+'/content').content==png()


def test_same_resource_result_is_idempotent_and_conflicting_bytes_do_not_overwrite(system,alice):
    from homeai.resource_assets import store_asset
    app,manifest,actor=setup_images(system,alice,cloud=False)
    with system[2]() as db:
        task,body=make_task(app,db,actor)
        original=store_asset(app,db,actor,task,'stored-result',png(),'原结果.png','image',{'origin':'generated'})
        db.commit()
        replay=store_asset(app,db,actor,task,'stored-result',png(),'原结果.png','image',{'origin':'generated'})
        assert replay==original
        with pytest.raises(HTTPException) as conflict:
            store_asset(app,db,actor,task,'stored-result',png()+b'changed','不同结果.png','image',{'origin':'generated'})
        assert conflict.value.status_code==409
        assert db.scalar(select(func.count()).select_from(MediaUpload))==1
        assert db.scalar(select(func.count()).select_from(MediaJob))==1
        assert db.scalar(select(func.count()).select_from(Record))==1
    assert alice.request('GET','/api/v1/assets/'+original['parts'][0]['record_id']+'/content').content==png()
