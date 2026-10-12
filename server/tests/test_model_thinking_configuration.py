"""思考模式只属于家庭模型用途配置，不触发模型调用或更换 Provider 身份。"""
import pytest
from conftest import SignedClient
from test_cloud_gateway import setup_cloud
from homeai.db import Provider, scope
from homeai.model_routing import ModelConfiguration, fingerprint, save_verification, shared_key, verification
from homeai.security import Actor


def test_thinking_defaults_preserve_provider_behavior_for_new_and_old_configuration(system,alice):
    first=alice.request('GET','/api/v1/manage/models')
    assert first.status_code==200
    assert first.json()['configuration']['cloud_planner_thinking']=='provider_default'
    with system[2]() as db:
        db.add(ModelConfiguration(household_id='h1',configuration=system[0].state.vault.seal(
            {'cloud_planner':None,'household_daily_tokens':'unlimited','member_daily_tokens':'unlimited'},'h1:model-configuration')))
        db.commit()
    legacy=alice.request('GET','/api/v1/manage/models').json()['configuration']
    assert legacy['cloud_planner_thinking']=='provider_default'
    assert legacy['household_daily_tokens']==legacy['member_daily_tokens']=='unlimited'


@pytest.mark.parametrize('mode',['provider_default','disabled'])
def test_thinking_setting_preserves_unlimited_quotas_manifest_verification_and_shared_key(system,alice,mode):
    app=system[0].state
    manifest=setup_cloud(system,alice)
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        save_verification(db,'h1',manifest,{'text':True,'tools':True},('text','tools'));db.commit()
        before_manifest=db.get(Provider,manifest.id).manifest
        before_report=verification(db,'h1',manifest)
    config=alice.request('GET','/api/v1/manage/models').json()['configuration']
    config.update(cloud_planner_thinking=mode,household_daily_tokens='unlimited',member_daily_tokens='unlimited',
                  household_daily_images=5,member_daily_images=2)
    saved=alice.request('PUT','/api/v1/manage/models',config)
    assert saved.status_code==200,saved.text
    refreshed=alice.request('GET','/api/v1/manage/models')
    assert refreshed.json()['configuration']==config
    assert 'service-secret' not in refreshed.text
    bob=SignedClient(system[1],system[2])
    with system[2]() as db:
        scope(db,bob.user_id,'h1')
        assert db.get(Provider,manifest.id).manifest==before_manifest
        assert fingerprint(manifest)==fingerprint(type(manifest).model_validate_json(before_manifest))
        assert verification(db,'h1',manifest)==before_report
        actor=Actor(bob.user_id,'h1',bob.device_id,'adult')
        assert shared_key(app.vault,db,actor,manifest)=='service-secret'
        assert db.info['user_id']==bob.user_id


@pytest.mark.parametrize('mode',['enabled','auto','false',False,None])
def test_unknown_thinking_modes_cannot_be_saved(alice,mode):
    result=alice.request('PUT','/api/v1/manage/models',{'cloud_planner_thinking':mode})
    assert result.status_code==422
    assert alice.request('GET','/api/v1/manage/models').json()['configuration']['cloud_planner_thinking']=='provider_default'


def test_regular_member_cannot_change_household_thinking_mode(system,alice):
    bob=SignedClient(system[1],system[2])
    assert bob.request('PUT','/api/v1/manage/models',{'cloud_planner_thinking':'disabled'}).status_code==403
    assert alice.request('GET','/api/v1/manage/models').json()['configuration']['cloud_planner_thinking']=='provider_default'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['provider_default','disabled'])
async def test_gateway_uses_saved_thinking_mode_and_binds_it_to_real_payload(system,alice,monkeypatch,mode):
    import json
    import httpx
    from homeai import cloud_gateway
    from homeai.contracts import TaskRequest
    from homeai.runtime import submit
    from homeai.providers import Registry
    app=system[0].state;manifest=setup_cloud(system,alice)
    config=alice.request('GET','/api/v1/manage/models').json()['configuration']
    assert alice.request('PUT','/api/v1/manage/models',{**config,'cloud_planner_thinking':mode}).status_code==200
    seen=[]
    def send(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'公开答复'},'finish_reason':'stop'}],'usage':{'total_tokens':7}})
    app.registry=Registry(app.vault,httpx.MockTransport(send))
    async def check(*args,review=False):
        return {'safe':True,'uncertain':False} if review else {'entities':[],'sensitive':False,'uncertain':False}
    monkeypatch.setattr(cloud_gateway,'local_check',check)
    actor=Actor(alice.user_id,'h1',alice.device_id,'infrastructure_owner')
    with system[2]() as db:
        task=submit(db,actor,TaskRequest(message='公开样例',idempotency_key='thinking-payload-'+mode),app.vault);db.commit()
        body=app.vault.open(task.request,actor.user_id+':task:'+task.id)
        result=await cloud_gateway.invoke(app,db,actor,manifest,task,body,{'messages':[{'role':'user','content':'只回复OK'}],'max_tokens':100},'thinking-probe-'+mode)
        assert result['choices'][0]['message']['content']=='公开答复'
        assert body['_model_response_diagnostics'][-1]['finish_reason']=='stop'
        assert body['_cloud_token_charge']==7
    assert seen[0].get('thinking')==({'type':'disabled'} if mode=='disabled' else None)


@pytest.mark.asyncio
async def test_local_chat_disables_thinking_in_provider_payload(system,alice):
    import json,httpx
    from homeai.providers import Registry
    from homeai.contracts import ProviderManifest
    seen=[]
    def transport(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'message':{'content':'你好'}}]})
    manifest=ProviderManifest(id='local.no-thinking',version='1',adapter='openai',endpoint='http://127.0.0.1:58100/v1',model='local',allowed_hosts=['127.0.0.1'],capabilities={'model.generate@v1':'chat'})
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        await Registry(system[0].state.vault,httpx.MockTransport(transport)).invoke(db,Actor(alice.user_id,'h1',alice.device_id,'adult'),manifest,'model.generate@v1',{'messages':[{'role':'user','content':'你好'}],'thinking':{'type':'disabled'}},'no-thinking-test')
    assert seen[0]['thinking']=={'type':'disabled'}
    assert seen[0]['chat_template_kwargs']=={'enable_thinking':False}
