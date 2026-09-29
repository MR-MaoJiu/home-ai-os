"""仅隔离测试使用传输替身，验证出站前的强制边界。"""
import json
import pytest
import httpx
from fastapi import HTTPException
from conftest import SignedClient
from homeai.contracts import ProviderManifest, TaskRequest
from homeai.db import Provider, Secret, scope
from homeai.security import Actor
from homeai.crypto import canonical, digest
from homeai.providers import Registry
from homeai.model_routing import configuration, reserve, BudgetExceeded, ModelUsage, RedactionArtifact
from homeai.cloud_gateway import CloudPermit, check_permit, invoke, PrivacyUnavailable
from homeai.runtime import submit


def setup_cloud(system, alice):
    app, client, factory = system
    manifest = ProviderManifest(id='cloud.test', version='1', adapter='openai', endpoint='https://model.example/v1', cloud=True, model='test', secret_id='model-key', capabilities={'model.generate@v1':'chat'}, allowed_hosts=['model.example'])
    with factory() as db:
        db.add(Provider(id=manifest.id, manifest=manifest.model_dump_json(), enabled=True))
        db.add(Secret(id='model-key', owner_id=alice.user_id, household_id='h1', provider_id=manifest.id, value=app.state.vault.seal('service-secret',alice.user_id+':secret:model-key')))
        db.commit()
    result=alice.request('PUT','/api/v1/manage/models',{'cloud_planner':manifest.id,'shared_cloud':True,'household_daily_tokens':100000,'member_daily_tokens':50000})
    assert result.status_code==200,result.text
    return manifest


@pytest.mark.asyncio
async def test_raw_cloud_calls_and_changed_payload_are_blocked(system,alice):
    manifest=setup_cloud(system,alice)
    actor=Actor(alice.user_id,'h1',alice.device_id,'adult')
    registry=Registry(system[0].state.vault,httpx.MockTransport(lambda request:pytest.fail('不得出站')))
    with system[2]() as db:
        with pytest.raises(HTTPException) as error:
            await registry.invoke(db,actor,manifest,'model.generate@v1',{'messages':[]},'bypass')
        assert error.value.status_code==403
        from homeai.model_routing import fingerprint
        from homeai.db import now
        permit=CloudPermit(actor.user_id,fingerprint(manifest),digest(canonical({'messages':[]})),now()+30)
        with pytest.raises(HTTPException):
            check_permit(permit,actor,manifest,{'messages':[{'content':'changed'}]})


@pytest.mark.asyncio
async def test_household_key_does_not_change_member_and_context_is_redacted(system,alice,monkeypatch):
    from homeai import cloud_gateway
    from sqlalchemy import select
    manifest=setup_cloud(system,alice)
    bob=SignedClient(system[1],system[2])
    actor=Actor(bob.user_id,'h1',bob.device_id,'adult')
    seen=[]
    def transport(request):
        seen.append((request.headers.get('authorization'),json.loads(request.content)))
        return httpx.Response(200,json={'choices':[{'message':{'content':'完成'}}],'usage':{'total_tokens':10}})
    app=system[0].state
    app.registry=Registry(app.vault,httpx.MockTransport(transport))
    async def check(*args,review=False):return {'safe':True,'uncertain':False} if review else {'entities':['张三'],'sensitive':False,'uncertain':False}
    monkeypatch.setattr(cloud_gateway,'local_check',check)
    with system[2]() as db:
        scope(db,actor.user_id,actor.household_id)
        task=submit(db,actor,TaskRequest(message='公开测试',idempotency_key='public-test-123'),app.vault)
        body=app.vault.open(task.request,actor.user_id+':task:'+task.id)
        db.commit()
        result=await invoke(app,db,actor,manifest,task,body,{'messages':[{'role':'user','content':'张三的邮箱 test@example.com'},{'role':'tool','content':'历史邮箱 test@example.com'}],'max_tokens':100},'cloud-call-1')
        assert result['choices'][0]['message']['content']=='完成'
        assert db.info['user_id']==bob.user_id
        assert seen[0][0]=='Bearer service-secret'
        outgoing=json.dumps(seen[0][1],ensure_ascii=False)
        assert '张三' not in outgoing and 'test@example.com' not in outgoing
        assert db.scalar(select(ModelUsage)).owner_id==bob.user_id
        artifact=db.scalar(select(RedactionArtifact))
        assert 'test@example.com' not in artifact.payload
        assert app.vault.open(artifact.payload,bob.user_id+':redaction:'+artifact.id)['mapping']
    assert bob.request('GET','/api/v1/manage/models').status_code==403


@pytest.mark.asyncio
async def test_privacy_failure_prevents_network(system,alice,monkeypatch):
    from homeai import cloud_gateway
    manifest=setup_cloud(system,alice)
    actor=Actor(alice.user_id,'h1',alice.device_id,'adult')
    app=system[0].state
    app.registry=Registry(app.vault,httpx.MockTransport(lambda request:pytest.fail('不得出站')))
    async def broken(*args,**kwargs):raise PrivacyUnavailable()
    monkeypatch.setattr(cloud_gateway,'local_check',broken)
    with system[2]() as db:
        task=submit(db,actor,TaskRequest(message='测试',idempotency_key='privacy-failure-123'),app.vault)
        body=app.vault.open(task.request,actor.user_id+':task:'+task.id)
        with pytest.raises(PrivacyUnavailable):
            await invoke(app,db,actor,manifest,task,body,{'messages':[{'role':'user','content':'公开文字'}]},'failure-1')


def test_budget_reservations_are_idempotent_and_shared(system,alice):
    setup_cloud(system,alice)
    app=system[0].state
    actor=Actor(alice.user_id,'h1',alice.device_id,'adult')
    with system[2]() as db:
        scope(db,actor.user_id,'h1')
        task=submit(db,actor,TaskRequest(message='测试',idempotency_key='budget-test-123'),app.vault)
        reserve(app,db,actor,task,'budget-1',40000)
        db.commit()
        with pytest.raises(BudgetExceeded):reserve(app,db,actor,task,'budget-1',1)
        with pytest.raises(BudgetExceeded):reserve(app,db,actor,task,'budget-2',10001)


def test_sharing_requires_explicit_daily_limits(system,alice):
    setup_cloud(system,alice)
    result=alice.request('PUT','/api/v1/manage/models',{'cloud_planner':'cloud.test','shared_cloud':True})
    assert result.status_code==422
    result=alice.request('GET','/api/v1/manage/models')
    assert 'service-secret' not in result.text and '_service_credential' not in result.text


@pytest.mark.asyncio
async def test_public_endpoint_cannot_be_marked_local_to_bypass_gateway(system,alice):
    manifest=ProviderManifest(id='false.local',version='1',adapter='openai',endpoint='https://public.example/v1',model='x',capabilities={'model.generate@v1':'chat'},allowed_hosts=['public.example'])
    with system[2]() as db:
        with pytest.raises(HTTPException) as denied:
            await Registry(system[0].state.vault).invoke(db,Actor(alice.user_id,'h1',alice.device_id,'adult'),manifest,'model.generate@v1',{'messages':[]},'false-local')
        assert denied.value.status_code==403


def prepare_media_gateway(system, alice):
    """只替换本地检测模型和云HTTP传输，保留真实授权、脱敏、账本及AES路径。"""
    from test_media import upload, png
    manifest=setup_cloud(system,alice)
    app=system[0].state
    privacy=ProviderManifest(id='privacy.test',version='1',adapter='openai',endpoint='http://local.test/v1',model='privacy',
        capabilities={'model.generate@v1':'chat'},allowed_hosts=['local.test'])
    with system[2]() as db:
        from homeai.model_routing import ModelVerification,fingerprint
        db.add(Provider(id=privacy.id,manifest=privacy.model_dump_json(),enabled=True))
        db.add(ModelVerification(id='h1:'+privacy.id,household_id='h1',provider_id=privacy.id,fingerprint=fingerprint(privacy),report=json.dumps({'privacy':True,'text':True})))
        db.commit()
    traffic=[]
    def transport(request):
        body=json.loads(request.content);traffic.append((request.url.host,body))
        if request.url.host=='local.test':
            review='"safe"' in body['messages'][0]['content']
            value={'safe':True,'uncertain':False} if review else {'entities':['AA'],'sensitive':False,'uncertain':False}
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(value)}}]})
        assert request.url.host=='model.example'
        return httpx.Response(200,json={'choices':[{'message':{'content':'已分析已批准图像'}}],'usage':{'total_tokens':12}})
    app.registry=Registry(app.vault,httpx.MockTransport(transport))
    result=upload(alice,png(),'公开测试像素.png','image')
    assert result.status_code==200,result.text
    return manifest,result.json()['asset']['record_id'],traffic


@pytest.mark.asyncio
@pytest.mark.parametrize('expired',[False,True])
async def test_media_cloud_requires_current_consent_and_preserves_jpeg(system,alice,expired):
    from homeai.media import cloud_images
    from homeai.cloud_gateway import CloudConsentRequired
    from homeai.client_actions import create_request,ClientAction
    from homeai.db import Task,now
    manifest,rid,traffic=prepare_media_gateway(system,alice)
    app=system[0].state;actor=Actor(alice.user_id,'h1',alice.device_id,'infrastructure_owner')
    with system[2]() as db:
        images=await cloud_images(app,db,actor,rid)
        original_url=images[0]['image_url']['url']
        assert 'AA' in original_url
        args={'messages':[{'role':'user','content':[{'type':'text','text':'昵称是AA，请描述这张图片。'},*images]}],'max_tokens':128}
        task=submit(db,actor,TaskRequest(message='昵称是AA，请描述这张图片。',record_ids=[rid],idempotency_key='media-consent-test'),app.vault)
        tid=task.id;db.commit()
        body=app.vault.open(task.request,actor.user_id+':task:'+tid)
        with pytest.raises(CloudConsentRequired) as waiting:
            await invoke(app,db,actor,manifest,task,body,args,'media-cloud-call')
        assert not any(host=='model.example' for host,_ in traffic)
        consent=waiting.value
        request=create_request(app,db,actor,task,'cloud.disclose',consent.detail,
            {'scope_hash':consent.scope_hash,'record_versions':consent.record_versions,'provider_id':manifest.id},
            target_member_id=consent.target_member_id,request_key='media-consent')
        action_id=request['client_action_id'];db.commit()
    response=alice.request('POST','/api/v1/client-actions/'+action_id+'/respond',{'idempotency_key':'media-approval-123'})
    assert response.status_code==200,response.text
    traffic.clear()
    with system[2]() as db:
        scope(db,actor.user_id,actor.household_id)
        if expired:
            db.get(ClientAction,action_id).expires_at=now()-1;db.commit()
        task=db.get(Task,tid);body=app.vault.open(task.request,actor.user_id+':task:'+tid)
        if expired:
            with pytest.raises(HTTPException) as failure:
                await invoke(app,db,actor,manifest,task,body,args,'media-cloud-call')
            assert failure.value.status_code==403
            assert traffic==[]
        else:
            result=await invoke(app,db,actor,manifest,task,body,args,'media-cloud-call')
            assert result['choices'][0]['message']['content']=='已分析已批准图像'
            cloud=[body for host,body in traffic if host=='model.example']
            assert len(cloud)==1
            content=cloud[0]['messages'][0]['content']
            assert 'AA' not in content[0]['text']
            assert content[1]['image_url']['url']==original_url


@pytest.mark.asyncio
async def test_cloud_media_external_url_is_rejected_before_any_transport(system,alice):
    manifest=setup_cloud(system,alice)
    app=system[0].state;actor=Actor(alice.user_id,'h1',alice.device_id,'infrastructure_owner')
    app.registry=Registry(app.vault,httpx.MockTransport(lambda request:pytest.fail('外链媒体不得触发任何模型传输')))
    with system[2]() as db:
        task=submit(db,actor,TaskRequest(message='禁止外链',idempotency_key='external-media-test'),app.vault)
        body=app.vault.open(task.request,actor.user_id+':task:'+task.id)
        with pytest.raises(PrivacyUnavailable):
            await invoke(app,db,actor,manifest,task,body,{'messages':[{'role':'user','content':[{'type':'image_url','image_url':{'url':'https://external.invalid/private.jpg'}}]}]},'external-media-call')
