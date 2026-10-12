"""隔离隐私复核状态机：仅模拟模型 HTTP，不读取正式资料或声称真实云验收。"""
import copy
import json
import uuid

import httpx
import pytest
from sqlalchemy import func, select

from conftest import SignedClient
from homeai.db import Provider, Task, now, scope
from homeai.model_routing import ModelUsage
from homeai.providers import Registry
from homeai.runtime import run_task
from test_planning_resumption import setup_models, task_state
from test_security_data import put, record


def configure(system, alice, failure='uncertain'):
    cloud, _, _=setup_models(system,alice)
    control={'failure':failure,'network_failure':False,'cloud_reply':None}
    traffic=[]
    def transport(request):
        payload=json.loads(request.content)
        traffic.append((request.url.host,payload))
        if request.url.host=='fast.test':
            text='not-json' if control['failure']=='route_invalid' else json.dumps({'simple':False})
        elif request.url.host=='privacy.test':
            review='"safe"' in payload['messages'][0]['content']
            if control['failure']=='invalid_json':text='not-json'
            elif review:text=json.dumps({'safe':control['failure']!='review_refused','uncertain':False})
            else:text=json.dumps({'entities':[],'sensitive':False,'uncertain':control['failure']=='uncertain'})
        else:
            assert request.url.host=='model.example'
            if control['network_failure']:raise httpx.ReadTimeout('隔离测试中的未知响应',request=request)
            text=control['cloud_reply'] or '已完成本次公开说明。'
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':text}}],'usage':{'total_tokens':9}})
    system[0].state.registry=Registry(system[0].state.vault,httpx.MockTransport(transport))
    return cloud,control,traffic


def create(alice, record_ids=None, message='请解释公开的排队原理。'):
    response=alice.request('POST','/api/v1/tasks',{'idempotency_key':str(uuid.uuid4()),'message':message,'record_ids':record_ids or []})
    assert response.status_code==202,response.text
    return response.json()['id']


def review(user, task_id):
    response=user.request('GET','/api/v1/tasks/'+task_id+'/privacy-review')
    assert response.status_code==200,response.text
    return response.json()


def decide(user, task_id, item, decision, **extra):
    return user.request('POST','/api/v1/tasks/'+task_id+'/privacy-review',{
        'review_id':item['review_id'],'decision':decision,'idempotency_key':'privacy-test-'+str(uuid.uuid4()),**extra})


def cloud_requests(traffic):return [payload for host,payload in traffic if host=='model.example']


def assert_review_content_removed(system,user,task_id,*,context_changed=True):
    _,payload=task_state(system,user,task_id)
    snapshot=payload['_privacy_review']
    assert not {'original','candidate','mapping','detected'}&snapshot.keys()
    if context_changed:
        assert payload['_privacy_failure']['code']=='context_changed'
        assert payload['_privacy_failure']['overrideable'] is False


async def paused(system, alice, record_ids=None, message='请解释公开的排队原理。'):
    tid=create(alice,record_ids,message)
    await run_task(system[0].state,tid,alice.user_id)
    status,body=task_state(system,alice,tid)
    assert status=='WAITING_PRIVACY',status
    assert body.get('_model_rounds',0)==0 and body.get('_model_token_charge',0)==0
    return tid,review(alice,tid)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['uncertain','review_refused','invalid_json'])
async def test_review_explains_probability_failure_and_force_is_one_call(system,alice,failure):
    _,_,traffic=configure(system,alice,failure)
    tid,item=await paused(system,alice)
    assert item['review_id'] and item['code'] and item['reason'] and item['suggestion']
    assert item['stage'] in {'detect','review'}
    assert item['provider']['id']=='cloud.test'
    assert item['overrideable'] and item['can_force'] and item['expires_at']>now()
    assert item['blocks'] and cloud_requests(traffic)==[]
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,tid)
        body=system[0].state.vault.open(task.request,alice.user_id+':task:'+tid)
        frozen=copy.deepcopy(body['_privacy_review']['candidate'])
    request={'review_id':item['review_id'],'decision':'force','idempotency_key':'force-once-'+str(uuid.uuid4())}
    response=alice.request('POST','/api/v1/tasks/'+tid+'/privacy-review',request)
    assert response.status_code==200,response.text
    again=alice.request('POST','/api/v1/tasks/'+tid+'/privacy-review',request)
    assert again.status_code==200 and again.json()['replayed'] is True
    await run_task(system[0].state,tid,alice.user_id)
    status,body=task_state(system,alice,tid)
    assert status=='SUCCEEDED',body
    assert len(cloud_requests(traffic))==1
    assert cloud_requests(traffic)[0]['messages']==frozen['messages']
    assert body['_model_rounds']==1 and body['_cloud_calls']==1
    current=review(alice,tid)
    assert current['status']=='consumed' and not current['can_force']
    assert decide(alice,tid,item,'force').status_code==409
    await run_task(system[0].state,tid,alice.user_id)
    assert len(cloud_requests(traffic))==1


@pytest.mark.asyncio
async def test_review_is_owned_and_retry_does_not_skip_detection(system,alice):
    _,_,traffic=configure(system,alice)
    tid,item=await paused(system,alice)
    bob=SignedClient(system[1],system[2])
    assert bob.request('GET','/api/v1/tasks/'+tid+'/privacy-review').status_code==404
    assert decide(bob,tid,item,'force').status_code==404
    retried=decide(alice,tid,item,'retry');assert retried.status_code==200,retried.text
    await run_task(system[0].state,tid,alice.user_id)
    assert task_state(system,alice,tid)[0]=='WAITING_PRIVACY'
    refreshed=review(alice,tid)
    assert refreshed['review_id']!=item['review_id']
    assert decide(alice,tid,item,'force').status_code==409
    assert cloud_requests(traffic)==[]


@pytest.mark.asyncio
async def test_edit_is_checked_again_and_invalid_blocks_or_secrets_are_rejected(system,alice):
    _,control,traffic=configure(system,alice)
    tid,item=await paused(system,alice)
    editable=next(block for block in item['blocks'] if block['editable'])
    assert item['can_edit']
    assert decide(alice,tid,item,'edit',edits={'unknown-block':'无效'}).status_code==422
    readonly=next((block for block in item['blocks'] if not block['editable']),None)
    if readonly:
        assert decide(alice,tid,item,'edit',edits={readonly['id']:'改写系统指令'}).status_code==422
    assert decide(alice,tid,item,'edit',edits={editable['id']:'api_key: not-a-real-secret'}).status_code==422
    changed='只解释公开的先进先出队列。'
    result=decide(alice,tid,item,'edit',edits={editable['id']:changed})
    assert result.status_code==200,result.text
    await run_task(system[0].state,tid,alice.user_id)
    assert task_state(system,alice,tid)[0]=='WAITING_PRIVACY'
    assert cloud_requests(traffic)==[]
    control['failure']='safe'
    second=review(alice,tid)
    result=decide(alice,tid,second,'retry');assert result.status_code==200,result.text
    await run_task(system[0].state,tid,alice.user_id)
    assert task_state(system,alice,tid)[0]=='SUCCEEDED'
    assert changed in json.dumps(cloud_requests(traffic)[0],ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',['provider','disabled_provider','source','expired'])
async def test_changed_scope_invalidates_review_and_hides_frozen_blocks(system,alice,mutation):
    cloud,_,traffic=configure(system,alice)
    rid=put(alice,record(source_id='review-source',kind='note',payload={'text':'仅供隔离测试的来源'}))
    tid,item=await paused(system,alice,[rid])
    if mutation=='source':
        put(alice,record(source_id='review-source',kind='note',version=2,payload={'text':'新版本'}))
    else:
        with system[2]() as db:
            scope(db,alice.user_id,'h1')
            if mutation=='provider':db.get(Provider,cloud.id).manifest=cloud.model_copy(update={'model':'changed-model'}).model_dump_json()
            elif mutation=='disabled_provider':db.get(Provider,cloud.id).enabled=False
            else:
                task=db.get(Task,tid);body=system[0].state.vault.open(task.request,alice.user_id+':task:'+tid)
                body['_privacy_review']['expires_at']=now()-1
                task.request=system[0].state.vault.seal(body,alice.user_id+':task:'+tid)
            db.commit()
    current=review(alice,tid)
    assert current['status'] in {'invalidated','expired'}
    assert not current['can_force'] and not current['can_edit']
    assert current['blocks']==[]
    if mutation=='provider':
        assert '配置已变化' in current['reason'] and '停用' not in current['reason']
    if mutation=='disabled_provider':
        assert '已停用' in current['reason'] and '配置已变化' not in current['reason']
    assert_review_content_removed(system,alice,tid,context_changed=mutation!='expired')
    assert decide(alice,tid,item,'force').status_code==409
    assert cloud_requests(traffic)==[]


@pytest.mark.asyncio
async def test_force_waiting_budget_is_not_consumed_until_wire_is_sent(system,alice):
    _,_,traffic=configure(system,alice)
    tid,item=await paused(system,alice)
    config=alice.request('GET','/api/v1/manage/models').json()['configuration']
    response=alice.request('PUT','/api/v1/manage/models',{**config,'household_daily_tokens':1,'member_daily_tokens':1})
    assert response.status_code==200,response.text
    assert decide(alice,tid,item,'force').status_code==200
    await run_task(system[0].state,tid,alice.user_id)
    assert task_state(system,alice,tid)[0]=='WAITING_BUDGET'
    assert review(alice,tid)['status']=='approved' and cloud_requests(traffic)==[]
    with system[2]() as db:
        scope(db,alice.user_id,'h1');assert db.scalar(select(func.count()).select_from(ModelUsage))==0
    response=alice.request('PUT','/api/v1/manage/models',config);assert response.status_code==200,response.text
    assert alice.request('POST','/api/v1/tasks/'+tid+'/resume').status_code==200
    await run_task(system[0].state,tid,alice.user_id)
    assert task_state(system,alice,tid)[0]=='SUCCEEDED'
    assert review(alice,tid)['status']=='consumed' and len(cloud_requests(traffic))==1


@pytest.mark.asyncio
async def test_network_unknown_consumes_force_and_cannot_replay(system,alice):
    _,control,traffic=configure(system,alice)
    tid,item=await paused(system,alice)
    control['network_failure']=True
    assert decide(alice,tid,item,'force').status_code==200
    await run_task(system[0].state,tid,alice.user_id)
    assert task_state(system,alice,tid)[0]=='FAILED'
    assert review(alice,tid)['status']=='consumed'
    assert decide(alice,tid,item,'force').status_code==409
    await run_task(system[0].state,tid,alice.user_id)
    assert len(cloud_requests(traffic))==1


@pytest.mark.asyncio
async def test_routing_failure_has_no_outbound_snapshot_and_cannot_force(system,alice):
    _,_,traffic=configure(system,alice,'route_invalid')
    tid,item=await paused(system,alice)
    assert item['review_id'] is None
    assert not item['can_force'] and not item['can_edit'] and item['blocks']==[]
    assert decide(alice,tid,{'review_id':str(uuid.uuid4())},'force').status_code==409
    assert cloud_requests(traffic)==[]


@pytest.mark.asyncio
async def test_family_source_revocation_hides_review_and_blocks_force(system,alice):
    _,_,traffic=configure(system,alice)
    bob=SignedClient(system[1],system[2])
    rid=put(bob,record(source_id='shared-review',kind='note',payload={'text':'其他成员明确共享的隔离测试资料'}))
    assert bob.request('PUT','/api/v1/data/'+rid+'/visibility',{'visibility':'family'}).status_code==200
    tid,item=await paused(system,alice,[rid])
    assert bob.request('PUT','/api/v1/data/'+rid+'/visibility',{'visibility':'personal'}).status_code==200
    current=review(alice,tid)
    assert current['status']=='invalidated' and current['blocks']==[]
    assert_review_content_removed(system,alice,tid)
    assert not current['can_force'] and not current['can_edit']
    assert decide(alice,tid,item,'force').status_code in {403,404,409}
    assert cloud_requests(traffic)==[]


@pytest.mark.asyncio
async def test_force_cannot_override_secret_reclassification(system,alice):
    from homeai.db import Record
    _,_,traffic=configure(system,alice)
    rid=put(alice,record(source_id='secret-review',kind='note',payload={'text':'分类前的隔离测试资料'}))
    tid,item=await paused(system,alice,[rid])
    with system[2]() as db:
        scope(db,alice.user_id,'h1');db.get(Record,rid).sensitivity='SECRET';db.commit()
    current=review(alice,tid)
    assert current['status']=='invalidated' and not current['can_force'] and current['blocks']==[]
    assert_review_content_removed(system,alice,tid)
    assert decide(alice,tid,item,'force').status_code in {403,404,409}
    assert cloud_requests(traffic)==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('owner_decision',['deny','approve'])
async def test_force_does_not_replace_other_members_cloud_consent(system,alice,owner_decision):
    _,_,traffic=configure(system,alice)
    bob=SignedClient(system[1],system[2])
    rid=put(bob,record(source_id='other-member-consent',kind='note',payload={'text':'仅在本人另行同意后才可披露的测试资料'}))
    assert bob.request('PUT','/api/v1/data/'+rid+'/visibility',{'visibility':'family'}).status_code==200
    tid,item=await paused(system,alice,[rid])
    assert decide(alice,tid,item,'force').status_code==200
    await run_task(system[0].state,tid,alice.user_id)
    assert task_state(system,alice,tid)[0]=='WAITING_CLIENT'
    assert cloud_requests(traffic)==[] and review(alice,tid)['status']=='approved'
    requests=bob.request('GET','/api/v1/client-actions').json()['items']
    action=next(action for action in requests if action['kind']=='cloud.disclose' and action['status']=='PENDING')
    assert action['parameters']['record_versions']=={rid:1}
    assert alice.request('POST','/api/v1/client-actions/'+action['id']+'/respond',{'idempotency_key':'cannot-approve-for-bob'}).status_code==404
    if owner_decision=='deny':
        rejected=bob.request('POST','/api/v1/client-actions/'+action['id']+'/deny',{'idempotency_key':'bob-denies-disclosure'})
        assert rejected.status_code==200,rejected.text
        await run_task(system[0].state,tid,alice.user_id)
        assert task_state(system,alice,tid)[0]=='FAILED' and cloud_requests(traffic)==[]
    else:
        approved=bob.request('POST','/api/v1/client-actions/'+action['id']+'/respond',{'idempotency_key':'bob-approves-disclosure'})
        assert approved.status_code==200,approved.text
        await run_task(system[0].state,tid,alice.user_id)
        if task_state(system,alice,tid)[0]=='WAITING_CLIENT':
            own_actions=alice.request('GET','/api/v1/client-actions').json()['items']
            own_action=next(action for action in own_actions if action['kind']=='cloud.disclose' and action['status']=='PENDING')
            assert alice.request('POST','/api/v1/client-actions/'+own_action['id']+'/respond',{'idempotency_key':'alice-confirms-task'}).status_code==200
            await run_task(system[0].state,tid,alice.user_id)
        assert task_state(system,alice,tid)[0]=='SUCCEEDED',alice.request('GET','/api/v1/tasks/'+tid).json()
        assert len(cloud_requests(traffic))==1 and review(alice,tid)['status']=='consumed'
        with system[2]() as db:
            scope(db,alice.user_id,'h1');assert db.scalar(select(func.count()).select_from(ModelUsage))==1


@pytest.mark.asyncio
async def test_edit_removed_placeholder_cannot_restore_deleted_private_value(system,alice):
    _,control,traffic=configure(system,alice,'review_refused')
    address='testperson@example.invalid'
    tid,item=await paused(system,alice,message='请解释 '+address+' 的排队记录。')
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,tid)
        body=system[0].state.vault.open(task.request,alice.user_id+':task:'+tid)
        original_mapping=body['_privacy_review']['mapping']
        token=next(key for key,value in original_mapping.items() if value==address)
    editable=next(block for block in item['blocks'] if block['editable'] and token in block['text'])
    assert decide(alice,tid,item,'edit',edits={editable['id']:'伪造占位符 <PRIVATE_aaaaaaaaaaaa_9999>'}).status_code==422
    control['failure']='safe'
    control['cloud_reply']=token
    changed='只解释公开的队列调度规则。'
    response=decide(alice,tid,item,'edit',edits={editable['id']:changed})
    assert response.status_code==200,response.text
    await run_task(system[0].state,tid,alice.user_id)
    result=alice.request('GET','/api/v1/tasks/'+tid).json()
    assert result['status']=='SUCCEEDED',result
    assert len(cloud_requests(traffic))==1
    outgoing=json.dumps(cloud_requests(traffic)[0]['messages'],ensure_ascii=False)
    assert changed in outgoing and address not in outgoing and token not in outgoing
    returned=result['result']['choices'][0]['message']['content']
    assert address not in returned
    assert token in returned


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',['arguments','call_key'])
async def test_force_is_bound_to_original_arguments_and_call_key(system,alice,mutation):
    _,_,traffic=configure(system,alice)
    tid,item=await paused(system,alice)
    assert decide(alice,tid,item,'force').status_code==200
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,tid)
        body=system[0].state.vault.open(task.request,alice.user_id+':task:'+tid)
        if mutation=='arguments':body['message']='这已经是另一项请求，不能借用旧批准。'
        else:body['_model_rounds']=body.get('_model_rounds',0)+1
        task.request=system[0].state.vault.seal(body,alice.user_id+':task:'+tid);db.commit()
    await run_task(system[0].state,tid,alice.user_id)
    assert task_state(system,alice,tid)[0]=='FAILED'
    assert cloud_requests(traffic)==[]
    with system[2]() as db:
        scope(db,alice.user_id,'h1');assert db.scalar(select(func.count()).select_from(ModelUsage))==0


@pytest.mark.asyncio
async def test_force_cannot_override_policy_denial(system,alice):
    from homeai.policy import Policy
    _,_,traffic=configure(system,alice)
    tid,item=await paused(system,alice)
    assert decide(alice,tid,item,'force').status_code==200
    system[0].state.policy=Policy('http://opa.test',httpx.MockTransport(lambda request:httpx.Response(200,json={'result':False})))
    await run_task(system[0].state,tid,alice.user_id)
    assert task_state(system,alice,tid)[0]=='FAILED'
    assert cloud_requests(traffic)==[]


@pytest.mark.asyncio
async def test_new_non_gateway_failure_is_not_shadowed_by_old_consumed_review(system,alice):
    _,_,traffic=configure(system,alice)
    tid,item=await paused(system,alice)
    assert decide(alice,tid,item,'force').status_code==200
    await run_task(system[0].state,tid,alice.user_id)
    assert review(alice,tid)['status']=='consumed'
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,tid)
        body=system[0].state.vault.open(task.request,alice.user_id+':task:'+tid)
        task.status='WAITING_PRIVACY';task.error='后续分类结果无效'
        body['_privacy_failure']={'code':'routing_invalid_response','stage':'route','reason':'后续分类结果无效',
            'suggestion':'检查本地模型后重试','retryable':True,'overrideable':False}
        task.request=system[0].state.vault.seal(body,alice.user_id+':task:'+tid);db.commit()
    current=review(alice,tid)
    assert current['review_id'] is None and current['status']=='unavailable'
    assert current['reason']=='后续分类结果无效' and current['blocks']==[]
    assert not current['can_force'] and not current['can_edit']
    assert decide(alice,tid,item,'force').status_code==409
    assert len(cloud_requests(traffic))==1


@pytest.mark.asyncio
@pytest.mark.parametrize('review_status,task_status,canceled_count',[
    ('pending','WAITING_PRIVACY',1),('approved','RECEIVED',1),('consumed','SUCCEEDED',0),
])
@pytest.mark.parametrize('decision',['force','retry','edit'])
async def test_restore_removes_every_privacy_snapshot_and_decision(system,alice,review_status,task_status,canceled_count,decision):
    from homeai.backup import invalidate_restored_authorizations
    from homeai.crypto import canonical,digest
    from homeai.db import Invocation,uid
    _,_,traffic=configure(system,alice)
    tid,item=await paused(system,alice)
    unrelated=create(alice)
    replay={'review_id':item['review_id'],'decision':decision,'idempotency_key':'old-backup-privacy-decision'}
    invocation_id=uid()
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,tid)
        payload=system[0].state.vault.open(task.request,alice.user_id+':task:'+tid)
        payload['_privacy_review'].update(status=review_status,decision=decision)
        # 覆盖旧快照仍包含正文和已处理回执的恢复情形；不进行真实云调用。
        payload['_privacy_review_submissions']=[{'key_hash':digest(replay['idempotency_key'].encode()),
            'request_hash':digest(canonical({**replay,'edits':{}})),
            'result':{'review_id':item['review_id'],'status':'RECEIVED','decision':decision,'replayed':False}}]
        task.request=system[0].state.vault.seal(payload,alice.user_id+':task:'+tid)
        task.status=task_status
        task.result=system[0].state.vault.seal({'text':'旧复核相关结果'},alice.user_id+':task-result:'+tid)
        db.add(Invocation(id=invocation_id,owner_id=alice.user_id,household_id='h1',task_id=tid,step=0,capability='model.generate@v1',
            arguments_hash='old-arguments',arguments=system[0].state.vault.seal({},alice.user_id+':invocation:'+invocation_id),status='SUCCEEDED',
            result=system[0].state.vault.seal({'text':'旧调用返回'},alice.user_id+':invocation-result:'+invocation_id)))
        db.commit()
    counts=invalidate_restored_authorizations(system[2],system[0].state.vault)
    assert counts=={'revoked_grants':0,'canceled_requests':0,'revoked_cloud_consents':0,'canceled_tasks':canceled_count}
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,tid)
        payload=system[0].state.vault.open(task.request,alice.user_id+':task:'+tid)
        assert not {'_privacy_review','_privacy_review_submissions','_privacy_failure'}&payload.keys()
        assert task.cancel_requested and task.result is None
        assert task.status==('CANCELED' if canceled_count else 'SUCCEEDED')
        if canceled_count:assert '重新发起' in task.error
        assert db.get(Invocation,invocation_id).result is None
        assert db.get(Task,unrelated).status=='RECEIVED' and not db.get(Task,unrelated).cancel_requested
    assert alice.request('POST','/api/v1/tasks/'+tid+'/privacy-review',replay).status_code==409
    assert alice.request('POST','/api/v1/tasks/'+tid+'/resume').status_code==409
    await run_task(system[0].state,tid,alice.user_id)
    assert cloud_requests(traffic)==[]
    again=invalidate_restored_authorizations(system[2],system[0].state.vault)
    assert again['canceled_tasks']==0
