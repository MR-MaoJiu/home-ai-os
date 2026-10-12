"""隔离管理端配置与探测回执；不调用真实收费图片服务。"""
import pytest
from fastapi import HTTPException
from conftest import SignedClient
from homeai.contracts import ProviderManifest
from homeai.db import Provider, Secret, scope
from homeai.model_routing import ModelConfiguration, configuration, resolve_role, shared_key, verification, save_verification
from homeai.security import Actor


def image_provider(system, identifier='cloud.images', *, capabilities=None, adapter='openai', secret=None):
    manifest=ProviderManifest(id=identifier,version='1',adapter=adapter,endpoint='https://images.example.invalid/v1',
        model='ImageModel-Custom',cloud=True,secret_id=secret,allowed_hosts=['images.example.invalid'],
        capabilities=capabilities or {'image.generate@v1':'generations','image.edit@v1':'edits'})
    with system[2]() as db:
        db.add(Provider(id=manifest.id,manifest=manifest.model_dump_json(),enabled=True));db.commit()
    return manifest


def actor_for(alice, household='h1'):
    return Actor(alice.user_id,household,alice.device_id,'infrastructure_owner')


def test_image_roles_and_quotas_are_opt_in_without_false_verification(system,alice):
    manifest=image_provider(system)
    initial=alice.request('GET','/api/v1/manage/models').json()
    assert initial['configuration']['image_generation'] is None
    assert initial['configuration']['image_editing'] is None
    assert initial['configuration']['household_daily_images']==0
    assert initial['configuration']['member_daily_images']==0
    report=next(item for item in initial['providers'] if item['id']==manifest.id)
    assert report['status']=='configured' and report['verified_at'] is None
    assert not report['vision'] and not report['image_generation'] and not report['image_editing']
    result=alice.request('PUT','/api/v1/manage/models',{'image_generation':manifest.id,'image_editing':manifest.id,
        'household_daily_images':4,'member_daily_images':2})
    assert result.status_code==200,result.text
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        config=configuration(system[0].state,db,'h1')
        assert config['household_daily_images']==4 and config['member_daily_images']==2
        # 首次实际请求可按明确配置路由，但不能假称已经实测。
        assert resolve_role(system[0].state,db,actor_for(alice),'image_generation').id==manifest.id
        assert resolve_role(system[0].state,db,actor_for(alice),'image_editing').id==manifest.id
        assert verification(db,'h1',manifest)['verified_at'] is None
        db.get(Provider,manifest.id).enabled=False;db.commit()
        with pytest.raises(HTTPException) as disabled:
            resolve_role(system[0].state,db,actor_for(alice),'image_generation')
        assert disabled.value.status_code==503


def test_legacy_model_configuration_gets_zero_image_limits(system,alice):
    with system[2]() as db:
        db.add(ModelConfiguration(household_id='h1',configuration=system[0].state.vault.seal(
            {'vision':None,'cloud_planner':None,'shared_cloud':False,'household_daily_tokens':0,'member_daily_tokens':0},'h1:model-configuration')))
        db.commit()
    config=alice.request('GET','/api/v1/manage/models').json()['configuration']
    assert config['image_generation'] is None and config['image_editing'] is None
    assert config['household_daily_images']==config['member_daily_images']==0


@pytest.mark.parametrize(('role','capabilities','adapter'),[
    ('image_generation',{'model.generate@v1':'chat'},'openai'),
    ('image_editing',{'image.generate@v1':'generations'},'openai'),
    ('vision',{'image.edit@v1':'edits'},'openai'),
    ('image_generation',{'image.generate@v1':'generations'},'http'),
])
def test_image_and_vision_roles_cannot_be_confused(system,alice,role,capabilities,adapter):
    manifest=image_provider(system,capabilities=capabilities,adapter=adapter)
    response=alice.request('PUT','/api/v1/manage/models',{role:manifest.id})
    assert response.status_code==422


@pytest.mark.parametrize(('field','value'),[
    ('household_daily_images',-1),('household_daily_images',10001),
    ('member_daily_images',-1),('member_daily_images',1001),
])
def test_image_quota_bounds(alice,field,value):
    assert alice.request('PUT','/api/v1/manage/models',{field:value}).status_code==422


@pytest.mark.parametrize('body',[
    {},{'generate':True},{'generate':True,'confirm_usage':False},{'confirm_usage':True},
])
def test_image_probe_requires_explicit_cost_confirmation(system,alice,monkeypatch,body):
    from homeai import image_generation
    manifest=image_provider(system)
    async def forbidden(*args,**kwargs):pytest.fail('未明确确认不得触发收费探测')
    monkeypatch.setattr(image_generation,'probe',forbidden)
    assert alice.request('POST','/api/v1/manage/models/'+manifest.id+'/verify-images',body).status_code==422


def test_image_probe_is_owner_only_and_does_not_call_text_or_unmapped_capability(system,alice,monkeypatch):
    from homeai import image_generation,cloud_gateway
    manifest=image_provider(system,capabilities={'image.generate@v1':'generations'})
    async def forbidden(*args,**kwargs):pytest.fail('权限或能力不匹配不得调用任何模型')
    monkeypatch.setattr(image_generation,'probe',forbidden)
    monkeypatch.setattr(cloud_gateway,'probe',forbidden)
    bob=SignedClient(system[1],system[2])
    assert bob.request('POST','/api/v1/manage/models/'+manifest.id+'/verify-images',{'generate':True,'confirm_usage':True}).status_code==403
    assert alice.request('POST','/api/v1/manage/models/'+manifest.id+'/verify-images',{'edit':True,'confirm_usage':True}).status_code==422
    assert alice.request('POST','/api/v1/manage/models/'+manifest.id+'/verify').status_code==422


def test_separate_image_verification_preserves_only_actually_tested_results(system,alice,monkeypatch):
    from homeai import image_generation,cloud_gateway
    manifest=image_provider(system,capabilities={'model.generate@v1':'chat','image.generate@v1':'generations','image.edit@v1':'edits'})
    seen=[]
    async def image_probe(app,db,actor,supplied,*,generate,edit):
        seen.append((supplied.id,generate,edit))
        return {'image_generation':generate,'image_editing':edit,'status':'verified','errors':{}}
    async def text_probe(*args):return {'text':True,'tools':True,'vision':True,'privacy':False,'status':'verified','errors':{}}
    monkeypatch.setattr(image_generation,'probe',image_probe)
    monkeypatch.setattr(cloud_gateway,'probe',text_probe)
    path='/api/v1/manage/models/'+manifest.id
    first=alice.request('POST',path+'/verify-images',{'generate':True,'confirm_usage':True})
    assert first.status_code==200,first.text
    assert first.json()['image_generation'] and not first.json()['image_editing'] and not first.json()['text']
    assert first.json()['verified_at'] is not None
    second=alice.request('POST',path+'/verify').json()
    assert second['text'] and second['vision'] and second['image_generation'] and not second['image_editing']
    third=alice.request('POST',path+'/verify-images',{'edit':True,'confirm_usage':True}).json()
    assert third['text'] and third['image_generation'] and third['image_editing']
    assert seen==[(manifest.id,True,False),(manifest.id,False,True)]
    # 修改模型名称后，之前的图片能力实测不得沿用。
    with system[2]() as db:
        changed=manifest.model_copy(update={'model':'Different-ImageModel'})
        db.get(Provider,manifest.id).manifest=changed.model_dump_json();db.commit()
    current=alice.request('GET','/api/v1/manage/models').json()['providers'][0]
    assert current['status']=='configured' and current['verified_at'] is None
    assert not current['text'] and not current['image_generation'] and not current['image_editing']


def test_image_reverification_failure_removes_old_success(system,alice,monkeypatch):
    from homeai import image_generation
    manifest=image_provider(system)
    with system[2]() as db:
        save_verification(db,'h1',manifest,{'image_generation':True},('image_generation',));db.commit()
    async def failed(*args,**kwargs):return {'image_generation':False,'status':'unavailable','errors':{'image_generation':'timeout'}}
    monkeypatch.setattr(image_generation,'probe',failed)
    result=alice.request('POST','/api/v1/manage/models/'+manifest.id+'/verify-images',{'generate':True,'confirm_usage':True})
    assert result.status_code==200,result.text
    assert result.json()['status']=='unavailable'
    assert not result.json()['image_generation'] and result.json()['errors']['image_generation']=='timeout'


def test_shared_image_credentials_are_explicit_and_never_returned(system,alice):
    image=image_provider(system,secret='image-secret')
    planner=image_provider(system,'cloud.planner',capabilities={'model.generate@v1':'chat'},secret='planner-secret')
    app=system[0].state
    with system[2]() as db:
        for manifest,value in ((image,'image-key-not-public'),(planner,'planner-key-not-public')):
            db.add(Secret(id=manifest.secret_id,owner_id=alice.user_id,household_id='h1',provider_id=manifest.id,
                value=app.vault.seal(value,alice.user_id+':secret:'+manifest.secret_id)))
        db.commit()
    config={'cloud_planner':planner.id,'image_generation':image.id,'image_editing':image.id,
        'shared_cloud':True,'household_daily_tokens':1000,'member_daily_tokens':100,
        'household_daily_images':4,'member_daily_images':2}
    result=alice.request('PUT','/api/v1/manage/models',config)
    assert result.status_code==200,result.text
    response=alice.request('GET','/api/v1/manage/models')
    assert 'not-public' not in response.text and '_service_credential' not in response.text
    bob=SignedClient(system[1],system[2])
    with system[2]() as db:
        scope(db,bob.user_id,'h1')
        assert shared_key(app.vault,db,actor_for(bob),image)=='image-key-not-public'
        assert shared_key(app.vault,db,actor_for(bob,'other-family'),image) is None
        assert db.info['user_id']==bob.user_id
    assert bob.request('GET','/api/v1/manage/models').status_code==403
    config['shared_cloud']=False
    assert alice.request('PUT','/api/v1/manage/models',config).status_code==200
    with system[2]() as db:
        scope(db,bob.user_id,'h1')
        assert shared_key(app.vault,db,actor_for(bob),image) is None
