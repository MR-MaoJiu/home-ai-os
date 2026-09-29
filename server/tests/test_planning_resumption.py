"""隔离状态机回归：HTTP传输替身只提供固定模型响应，不作为真实模型验收。"""
import json
import uuid

import httpx
import pytest
from sqlalchemy import select, func

from homeai.contracts import ProviderManifest
from homeai.db import Provider, Task, scope
from homeai.model_routing import ModelUsage, ModelVerification, fingerprint
from homeai.providers import Registry
from homeai.runtime import run_task
from homeai.security import Actor
from homeai.conversations import history
from homeai.media import MediaJob, process_user
from test_cloud_gateway import setup_cloud
from test_conversations import new_chat, send, finish
from test_media import upload, png


def setup_models(system, alice):
    cloud=setup_cloud(system,alice)
    fast=ProviderManifest(id='fast.test',version='1',adapter='openai',endpoint='http://fast.test/v1',model='fast',allowed_hosts=['fast.test'],capabilities={'model.generate@v1':'chat'})
    privacy=ProviderManifest(id='privacy.test',version='1',adapter='openai',endpoint='http://privacy.test/v1',model='privacy',allowed_hosts=['privacy.test'],capabilities={'model.generate@v1':'chat'})
    with system[2]() as db:
        for manifest in (fast,privacy):db.add(Provider(id=manifest.id,manifest=manifest.model_dump_json(),enabled=True))
        for manifest in (fast,privacy,cloud):
            db.add(ModelVerification(id='h1:'+manifest.id,household_id='h1',provider_id=manifest.id,fingerprint=fingerprint(manifest),report=json.dumps({'text':True,'tools':True,'vision':manifest.cloud,'privacy':not manifest.cloud,'status':'verified'})))
        db.commit()
    response=alice.request('PUT','/api/v1/manage/models',{'local_fast':fast.id,'local_privacy':privacy.id,'cloud_planner':cloud.id,'vision':cloud.id,'shared_cloud':True,'household_daily_tokens':100000,'member_daily_tokens':50000})
    assert response.status_code==200,response.text
    traffic=[]
    def transport(request):
        payload=json.loads(request.content);traffic.append((request.url.host,payload))
        if request.url.host=='fast.test':
            content=json.dumps({'simple':False})
        elif request.url.host=='privacy.test':
            assert payload.get('response_format',{}).get('type')=='json_schema'
            review='"safe"' in payload['messages'][0]['content']
            content=json.dumps({'safe':True,'uncertain':False} if review else {'entities':[],'sensitive':False,'uncertain':False})
        else:
            assert request.url.host=='model.example'
            content='状态机测试的固定答复'
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':content}}],'usage':{'total_tokens':8}})
    system[0].state.registry=Registry(system[0].state.vault,httpx.MockTransport(transport))
    return cloud,privacy,traffic


def task_state(system,user,identifier):
    with system[2]() as db:
        scope(db,user.user_id,'h1');task=db.get(Task,identifier)
        return task.status,system[0].state.vault.open(task.request,user.user_id+':task:'+task.id)


def media_message(user,conversation,assets):
    response=user.request('POST','/api/v1/conversations/'+conversation+'/messages',{
        'client_key':str(uuid.uuid4()),'content':'请分析这些附件。',
        'parts':[{'type':item['kind'],'record_id':item['record_id'],'version':item['version']} for item in assets]})
    assert response.status_code==202,response.text
    return response.json()


@pytest.mark.asyncio
async def test_waiting_media_with_cloud_vision_resumes_after_only_pending_text_finishes(system,alice):
    from homeai.worker import cycle
    _,_,traffic=setup_models(system,alice)
    text=upload(alice,'文本附件：周末检查备份。'.encode()).json()['asset']
    picture=upload(alice,png(),'picture.png','image').json()['asset']
    turn=media_message(alice,new_chat(alice),[text,picture]);tid=turn['task_id']
    await run_task(system[0].state,tid,alice.user_id)
    status,body=task_state(system,alice,tid)
    assert status=='WAITING_MEDIA'
    assert body['_media_wait_record_id']==text['record_id']
    assert traffic==[]
    # 实际内置UTF-8解析只完成第一个文件；不伪造视觉结果或把图片任务标成成功。
    await process_user(system[0].state,alice.user_id,'h1')
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        assert db.scalar(select(MediaJob).where(MediaJob.record_id==text['record_id'])).status=='succeeded'
        assert db.scalar(select(MediaJob).where(MediaJob.record_id==picture['record_id'])).status=='queued'
    await cycle(system[0].state)
    status,body=task_state(system,alice,tid)
    assert status=='WAITING_CLIENT',status
    assert body['_client_wait']['action_id']
    assert not any(host=='model.example' for host,_ in traffic)
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        assert db.scalar(select(MediaJob).where(MediaJob.record_id==picture['record_id'])).status=='queued'


def test_history_snapshot_excludes_task_that_completed_after_message_creation(system,alice):
    from homeai.client_actions import create_request
    identifier=new_chat(alice)
    first=send(alice,identifier,'先等待我补充内容').json()
    actor=Actor(alice.user_id,'h1',alice.device_id,'infrastructure_owner')
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,first['task_id'])
        create_request(system[0].state,db,actor,task,'provide_text','请补充内容',{'max_length':100},request_key='snapshot-wait')
        db.commit()
    second=send(alice,identifier,'这是等待期间提出的新问题').json()
    finish(system,alice,first['task_id'],'迟到的第一轮回答')
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,second['task_id'])
        body=system[0].state.vault.open(task.request,alice.user_id+':task:'+task.id)
        assert body['_history_snapshot']==[]
        assert history(system[0].state,db,actor,body)==[]
    finish(system,alice,second['task_id'],'第二轮已完成')
    third=send(alice,identifier,'现在开启后续问题').json()
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,third['task_id'])
        body=system[0].state.vault.open(task.request,alice.user_id+':task:'+task.id)
        current=history(system[0].state,db,actor,body)
        assert any('迟到的第一轮回答' in item['content'] for item in current)
        assert any('第二轮已完成' in item['content'] for item in current)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['disabled','unverified'])
async def test_privacy_unavailable_pauses_without_cloud_then_resumes(system,alice,failure):
    _,privacy,traffic=setup_models(system,alice)
    with system[2]() as db:
        if failure=='disabled':db.get(Provider,privacy.id).enabled=False
        else:db.get(ModelVerification,'h1:'+privacy.id).report=json.dumps({'text':True,'privacy':False})
        db.commit()
    turn=send(alice,new_chat(alice),'请检索公开资料后回答。').json();tid=turn['task_id']
    await run_task(system[0].state,tid,alice.user_id)
    status,body=task_state(system,alice,tid)
    assert status=='WAITING_PRIVACY',status
    assert body.get('_cloud_calls',0)==0
    assert body.get('_model_rounds',0)==0 and body.get('_model_token_charge',0)==0
    assert not any(host=='model.example' for host,_ in traffic)
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        assert db.scalar(select(func.count()).select_from(ModelUsage))==0
        db.get(Provider,privacy.id).enabled=True
        db.get(ModelVerification,'h1:'+privacy.id).report=json.dumps({'text':True,'privacy':True,'status':'verified'})
        db.commit()
    resumed=alice.request('POST','/api/v1/tasks/'+tid+'/resume')
    assert resumed.status_code==200,resumed.text
    await run_task(system[0].state,tid,alice.user_id)
    status,_=task_state(system,alice,tid)
    assert status=='SUCCEEDED',status
    assert len([host for host,_ in traffic if host=='model.example'])==1


@pytest.mark.asyncio
async def test_consent_wait_preserves_planning_budget_and_does_not_replan_until_approved(system,alice):
    _,_,traffic=setup_models(system,alice)
    picture=upload(alice,png(),'picture.png','image').json()['asset']
    turn=media_message(alice,new_chat(alice),[picture]);tid=turn['task_id']
    await run_task(system[0].state,tid,alice.user_id)
    status,body=task_state(system,alice,tid)
    assert status=='WAITING_CLIENT'
    assert body.get('_model_rounds',0)==0 and body.get('_model_token_charge',0)==0
    assert body.get('_cloud_calls',0)==0 and body.get('_cloud_token_charge',0)==0
    action=body['_client_wait']['action_id'];requests_before=len(traffic)
    await run_task(system[0].state,tid,alice.user_id)
    await run_task(system[0].state,tid,alice.user_id)
    assert len(traffic)==requests_before
    status,body=task_state(system,alice,tid)
    assert status=='WAITING_CLIENT' and body.get('_model_rounds',0)==0 and body.get('_model_token_charge',0)==0
    with system[2]() as db:
        scope(db,alice.user_id,'h1');assert db.scalar(select(func.count()).select_from(ModelUsage))==0
    result=alice.request('POST','/api/v1/client-actions/'+action+'/respond',{'idempotency_key':'approve-media-plan'})
    assert result.status_code==200,result.text
    await run_task(system[0].state,tid,alice.user_id)
    status,body=task_state(system,alice,tid)
    assert status=='SUCCEEDED',status
    assert body['_model_rounds']==1 and body['_cloud_calls']==1
    assert body['_model_token_charge']==8 and body['_cloud_token_charge']==8
    assert len([host for host,_ in traffic if host=='model.example'])==1
