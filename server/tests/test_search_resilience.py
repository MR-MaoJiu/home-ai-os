"""搜索语义状态与持久Agent恢复测试；传输替身不算真实互联网搜索验收。"""
import json
import uuid
from urllib.parse import parse_qs

import httpx
import pytest

from homeai.contracts import ProviderManifest
from homeai.db import Provider,scope
from homeai.providers import Registry
from homeai.runtime import run_task
from homeai.search_results import normalize
from homeai.security import Actor


def entry(url='https://docs.python.org/3/',title='Python文档'):
    return {'url':url,'title':title,'content':'这里是公开文档摘要。','engine':'fixture-engine'}


def failures():return [['upstream-one','timeout'],['upstream-two','HTTP 429']]


async def invoke_search(system,alice,response):
    manifest=ProviderManifest(id='test.search',version='1',adapter='searxng',endpoint='http://search.fixture.invalid',
        allowed_hosts=['search.fixture.invalid'],capabilities={'web.search@v1':'search'})
    registry=Registry(system[0].state.vault,httpx.MockTransport(lambda _:httpx.Response(200,json=response)))
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        return await registry.invoke(db,Actor(alice.user_id,'h1',alice.device_id,'adult'),manifest,'web.search@v1',{'query':'Python documentation'},'search-test')


@pytest.mark.asyncio
async def test_valid_results_with_engine_failures_are_partial(system,alice):
    result=await invoke_search(system,alice,{'results':[entry()],'unresponsive_engines':failures()})
    assert result['status']=='partial'
    assert result['results'][0]['url']=='https://docs.python.org/3/'
    assert result['content_trust']=='untrusted_web' and result['warning']
    assert len(result['unresponsive_engines'])==2


@pytest.mark.asyncio
async def test_no_results_with_engine_failures_are_unavailable_not_old_exception(system,alice):
    result=await invoke_search(system,alice,{'results':[],'unresponsive_engines':failures()})
    assert result['status']=='unavailable' and result['results']==[]
    assert result['error_reason']=='upstream_incomplete' and result['warning']


@pytest.mark.asyncio
async def test_empty_without_engine_failure_is_a_valid_empty_search(system,alice):
    result=await invoke_search(system,alice,{'results':[],'unresponsive_engines':[]})
    assert result['status']=='empty' and result['results']==[]
    assert not result.get('warning') and not result.get('error_reason')


def test_url_safety_and_valid_results_after_fifty_invalid_entries():
    unsafe=['javascript:alert(1)','data:text/html,hello','file:///etc/passwd','ftp://example.com/file',
        'mailto:someone@example.com','/relative','https://','https://user:password@example.com/',
        'https://user@example.com/','http://[broken','https://example.com/'+'x'*4000,None,42]
    values=[entry(unsafe[index%len(unsafe)]) for index in range(65)]
    values.extend([entry(),entry(),entry('http://example.org/source','另一个来源')])
    result=normalize({'results':values,'unresponsive_engines':[]})
    assert result['status']=='ok'
    assert [item['url'] for item in result['results']]==['https://docs.python.org/3/','http://example.org/source']
    rejected=normalize({'results':[entry(value) for value in unsafe],'unresponsive_engines':[]})
    assert rejected['status']=='unavailable' and rejected['results']==[]
    assert rejected['error_reason']=='no_safe_results'


def tool(query):
    return {'choices':[{'message':{'content':'','tool_calls':[{'id':'model-proposal','type':'function',
        'function':{'name':'search_web','arguments':json.dumps({'query':query})}}]},'finish_reason':'tool_calls'}],
        'usage':{'total_tokens':1}}


def answer(content):
    # 顶层伪造来源用于验证最终引用始终来自真实工具回执，而非模型自填。
    return {'choices':[{'message':{'content':content},'finish_reason':'stop'}],'usage':{'total_tokens':1},
        'web_sources':[{'title':'模型自行编造的来源','url':'https://invented.invalid/source'}]}


def install_transport(system,model_outputs,search_outputs):
    captured={'models':0,'searches':[],'model_requests':[]}
    def handle(request):
        if request.url.host=='model.fixture.invalid':
            index=captured['models'];captured['models']+=1
            captured['model_requests'].append(json.loads(request.content))
            assert index<len(model_outputs),'Agent发起了意料之外的额外规划调用'
            return httpx.Response(200,json=model_outputs[index])
        assert request.url.host=='search.fixture.invalid'
        query=parse_qs(request.content.decode())['q'][0]
        captured['searches'].append(query)
        assert query in search_outputs,'Agent执行了未约定的搜索'
        return httpx.Response(200,json=search_outputs[query])
    system[0].state.registry=Registry(system[0].state.vault,httpx.MockTransport(handle))
    with system[2]() as db:
        manifests=[ProviderManifest(id='test.model',version='1',adapter='openai',endpoint='http://model.fixture.invalid/v1',model='state-test-model',allowed_hosts=['model.fixture.invalid'],capabilities={'model.generate@v1':'chat'}),
            ProviderManifest(id='test.search',version='1',adapter='searxng',endpoint='http://search.fixture.invalid',allowed_hosts=['search.fixture.invalid'],capabilities={'web.search@v1':'search'})]
        for manifest in manifests:db.add(Provider(id=manifest.id,manifest=manifest.model_dump_json(),enabled=True,health='test-transport'))
        db.commit()
    return captured


def submit(alice,max_steps=6,message='检索Python公开文档，并核对补充资料。'):
    response=alice.request('POST','/api/v1/tasks',{'idempotency_key':str(uuid.uuid4()),'message':message,
        'max_steps':max_steps,'max_output_tokens':256,'max_model_tokens':262144})
    assert response.status_code==202,response.text
    return response.json()['id']


async def step(system,alice,task_id):
    await run_task(system[0].state,task_id,alice.user_id)
    return alice.request('GET','/api/v1/tasks/'+task_id).json()


@pytest.mark.asyncio
async def test_agent_keeps_existing_sources_when_supplemental_search_is_unavailable(system,alice):
    source=entry();second=entry('https://www.python.org/about/','Python介绍')
    captured=install_transport(system,[tool('Python docs'),tool('Python supplemental'),answer('根据已获得的文档，Python提供公开语言参考。')],{
        'Python docs':{'results':[source,second],'unresponsive_engines':[]},
        'Python supplemental':{'results':[],'unresponsive_engines':failures()}})
    task_id=submit(alice)
    assert (await step(system,alice,task_id))['status']=='RECEIVED'  # 首次规划。
    assert (await step(system,alice,task_id))['status']=='RECEIVED'  # 实际第一步执行。
    assert (await step(system,alice,task_id))['status']=='RECEIVED'  # 补充规划。
    partial=await step(system,alice,task_id)
    assert partial['status']=='RECEIVED' and partial['error'] is None
    assert partial['result']['results'][0]['url']==source['url']
    rows=alice.request('GET','/api/v1/tasks/'+task_id+'/steps').json()
    assert [row['status'] for row in rows]==['SUCCEEDED','UNAVAILABLE']
    assert rows[1]['result']['status']=='unavailable'
    final=await step(system,alice,task_id)
    assert final['status']=='SUCCEEDED' and final['error'] is None
    result=final['result']
    assert {item['url'] for item in result['web_sources']}=={source['url'],second['url']}
    assert result['search_status']=='partial' and result['search_warnings']
    assert '搜索说明' in result['choices'][0]['message']['content']
    assert '已取得的有效来源' in result['choices'][0]['message']['content']
    assert captured['searches']==['Python docs','Python supplemental']


@pytest.mark.asyncio
async def test_all_searches_unavailable_never_report_task_success_or_invented_sources(system,alice):
    captured=install_transport(system,[tool('Python docs'),tool('Python supplemental'),answer('本次未能取得联网结果，无法核实。')],{
        'Python docs':{'results':[],'unresponsive_engines':failures()},
        'Python supplemental':{'results':[],'unresponsive_engines':failures()}})
    task_id=submit(alice)
    for _ in range(8):
        final=await step(system,alice,task_id)
        if final['status'] in {'SUCCEEDED','FAILED','CANCELED','NEEDS_RECONCILIATION'}:break
    assert final['status']=='FAILED' and '搜索' in final['error']
    result=final['result']
    assert result['web_sources']==[] and result['search_status']=='unavailable'
    assert result['search_warnings'] and '无法核实' in result['choices'][0]['message']['content']
    assert 'invented.invalid' not in json.dumps(result,ensure_ascii=False)
    rows=alice.request('GET','/api/v1/tasks/'+task_id+'/steps').json()
    assert len(rows)==2 and all(row['status']=='UNAVAILABLE' for row in rows)
    assert all(row['result']['results']==[] for row in rows)
    assert captured['searches']==['Python docs','Python supplemental']


@pytest.mark.asyncio
async def test_repeated_identical_search_is_not_dispatched_again(system,alice):
    captured=install_transport(system,[tool('Python docs'),tool('Python docs'),answer('依据已取得的来源回答。')],{
        'Python docs':{'results':[entry()],'unresponsive_engines':[]}})
    task_id=submit(alice)
    for _ in range(8):
        final=await step(system,alice,task_id)
        if final['status'] in {'SUCCEEDED','FAILED','CANCELED'}:break
    assert final['status']=='SUCCEEDED',final
    assert captured['searches']==['Python docs']
    assert len(alice.request('GET','/api/v1/tasks/'+task_id+'/steps').json())==1
    assert final['result']['web_sources'][0]['url']=='https://docs.python.org/3/'


def batch(*calls):
    return {'choices':[{'message':{'content':'','tool_calls':[{'id':'proposed-'+str(index),'type':'function',
        'function':{'name':name,'arguments':json.dumps(arguments)}} for index,(name,arguments) in enumerate(calls)]},
        'finish_reason':'tool_calls'}],'usage':{'total_tokens':1}}


def reminder_count(system,alice):
    from sqlalchemy import func,select
    from homeai.db import Record
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        return db.scalar(select(func.count()).select_from(Record).where(Record.owner_id==alice.user_id,Record.kind=='reminder.item'))


@pytest.mark.asyncio
async def test_unavailable_search_withdraws_same_batch_effect_then_replans_with_new_receipt(system,alice):
    first=batch(('search_web',{'query':'Python unavailable'}),('create_reminder',{'title':'检查资料'}))
    captured=install_transport(system,[first,tool('Python recovery'),batch(('create_reminder',{'title':'检查资料'})),answer('补充检索取得来源后，已创建提醒。')],{
        'Python unavailable':{'results':[],'unresponsive_engines':failures()},
        'Python recovery':{'results':[entry()],'unresponsive_engines':[]}})
    tid=submit(alice,max_steps=3,message='查找Python资料，再创建检查资料提醒。')
    assert (await step(system,alice,tid))['status']=='RECEIVED'
    assert (await step(system,alice,tid))['status']=='RECEIVED'
    rows=alice.request('GET','/api/v1/tasks/'+tid+'/steps').json()
    assert [row['status'] for row in rows]==['UNAVAILABLE','SKIPPED']
    original_id=rows[1]['id']
    assert rows[1]['result']=={'status':'not_executed','reason':'preceding_search_unavailable','blocked_by_step':0,'requires_replanning':True}
    assert reminder_count(system,alice)==0
    assert (await step(system,alice,tid))['status']=='RECEIVED'
    feedback=[json.loads(message['content']) for message in captured['model_requests'][1]['messages'] if message['role']=='tool']
    assert [value['status'] for value in feedback]==['unavailable','not_executed']
    assert reminder_count(system,alice)==0
    for _ in range(6):
        final=await step(system,alice,tid)
        if final['status'] in {'SUCCEEDED','FAILED'}:break
    assert final['status']=='SUCCEEDED',final
    assert reminder_count(system,alice)==1
    rows=alice.request('GET','/api/v1/tasks/'+tid+'/steps').json()
    assert [row['status'] for row in rows]==['UNAVAILABLE','SKIPPED','SUCCEEDED','SUCCEEDED']
    assert rows[1]['id']==original_id and rows[3]['id']!=original_id
    assert final['result']['search_status']=='partial'
    assert captured['searches']==['Python unavailable','Python recovery']


@pytest.mark.asyncio
async def test_restart_with_persisted_unavailable_withdraws_pending_approval_without_dispatch(system,alice):
    from sqlalchemy import select
    from homeai.api import create_app
    from homeai.db import Invocation,Approval,Task,uid,now,Audit
    from homeai.crypto import canonical,digest
    from homeai.search_results import unavailable
    first=batch(('search_web',{'query':'Python unavailable'}),('create_reminder',{'title':'不得提前创建'}))
    captured=install_transport(system,[first,answer('搜索不可用，后续提醒未执行。')],{})
    tid=submit(alice)
    await step(system,alice,tid)
    app=system[0].state
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,tid)
        search_id,pending_id=uid(),uid()
        db.add(Invocation(id=search_id,owner_id=alice.user_id,household_id='h1',task_id=tid,step=0,capability='web.search@v1',
            arguments_hash=digest(canonical({'query':'Python unavailable'})),arguments=app.vault.seal({'query':'Python unavailable'},alice.user_id+':invocation:'+search_id),
            status='UNAVAILABLE',result=app.vault.seal(unavailable('timeout'),alice.user_id+':invocation-result:'+search_id)))
        db.add(Invocation(id=pending_id,owner_id=alice.user_id,household_id='h1',task_id=tid,step=1,capability='reminder.create@v1',
            arguments_hash=digest(canonical({'title':'不得提前创建'})),arguments=app.vault.seal({'title':'不得提前创建'},alice.user_id+':invocation:'+pending_id),status='PENDING'))
        approval_id=uid();db.add(Approval(id=approval_id,owner_id=alice.user_id,household_id='h1',invocation_id=pending_id,arguments_hash='old',decision='APPROVED',expires_at=now()+300))
        task.status='EXECUTING';db.commit()
    # 重建应用实例，从同一持久数据库恢复旧批次，不依赖进程中的规划状态。
    recovered=create_app(app.settings,app.vault,system[2],app.policy).state
    recovered.registry=app.registry
    await run_task(recovered,tid,alice.user_id)
    final=alice.request('GET','/api/v1/tasks/'+tid).json()
    assert final['status']=='FAILED' and final['result']['search_status']=='unavailable'
    assert captured['searches']==[] and reminder_count(system,alice)==0
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        assert db.get(Invocation,pending_id).status=='SKIPPED'
        assert db.get(Approval,approval_id).decision=='CANCELED'
        assert len(list(db.scalars(select(Audit).where(Audit.action=='capability.plan_withdrawn',Audit.resource_id==pending_id))))==1
    await run_task(recovered,tid,alice.user_id)
    assert captured['models']==2 and reminder_count(system,alice)==0


@pytest.mark.asyncio
async def test_withdrawal_preserves_effect_executed_before_unavailable_search(system,alice):
    first=batch(('create_reminder',{'title':'明确的前置提醒'}),('search_web',{'query':'Python unavailable'}),('create_reminder',{'title':'未经重新规划的后置提醒'}))
    install_transport(system,[first],{'Python unavailable':{'results':[],'unresponsive_engines':failures()}})
    tid=submit(alice,message='创建前置提醒，搜索后再决定后置提醒。')
    await step(system,alice,tid);await step(system,alice,tid)
    original=alice.request('GET','/api/v1/tasks/'+tid+'/steps').json()[0]
    await step(system,alice,tid)
    rows=alice.request('GET','/api/v1/tasks/'+tid+'/steps').json()
    assert [row['status'] for row in rows]==['SUCCEEDED','UNAVAILABLE','SKIPPED']
    assert rows[0]==original and reminder_count(system,alice)==1


@pytest.mark.asyncio
async def test_withdrawn_read_can_be_replanned_and_skipped_render_is_not_projected(system,alice):
    first=batch(('search_web',{'query':'Python unavailable'}),('search_documents',{'query':'资料'}),
        ('render_table',{'title':'尚未执行','columns':['标题'],'rows':[['不能展示']]}))
    captured=install_transport(system,[first,batch(('search_documents',{'query':'资料'})),answer('未找到匹配资料，网络查询也暂不可用。')],{
        'Python unavailable':{'results':[],'unresponsive_engines':failures()}})
    tid=submit(alice)
    await step(system,alice,tid);await step(system,alice,tid)
    assert (await step(system,alice,tid))['status']=='RECEIVED'
    feedback=[json.loads(message['content']) for message in captured['model_requests'][1]['messages'] if message['role']=='tool']
    assert [item['status'] for item in feedback]==['unavailable','not_executed','not_executed']
    await step(system,alice,tid)
    final=await step(system,alice,tid)
    rows=alice.request('GET','/api/v1/tasks/'+tid+'/steps').json()
    assert [row['status'] for row in rows]==['UNAVAILABLE','SKIPPED','SKIPPED','SUCCEEDED']
    assert rows[3]['capability']=='knowledge.search@v1'
    assert final['result']['parts']==[] and final['status']=='FAILED'


@pytest.mark.asyncio
async def test_agent_valid_empty_search_is_reported_empty_and_can_finish(system,alice):
    install_transport(system,[tool('No matches'),answer('此次未查到匹配的公开资料。')],{
        'No matches':{'results':[],'unresponsive_engines':[]}})
    tid=submit(alice)
    await step(system,alice,tid);await step(system,alice,tid)
    final=await step(system,alice,tid)
    assert final['status']=='SUCCEEDED' and final['error'] is None
    assert final['result']['search_status']=='empty'
    assert final['result']['web_sources']==[] and final['result']['search_warnings']==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('interrupt',['revoked','expired'])
async def test_search_unavailable_preserves_device_cancellation_or_expired_deadline(system,alice,interrupt):
    from sqlalchemy import select
    from homeai.db import Device,Task,Invocation,now
    manifest=ProviderManifest(id='interrupt.search',version='1',adapter='searxng',endpoint='http://search.fixture.invalid',
        allowed_hosts=['search.fixture.invalid'],capabilities={'web.search@v1':'search'})
    with system[2]() as db:
        db.add(Provider(id=manifest.id,manifest=manifest.model_dump_json(),enabled=True));db.commit()
    created=alice.request('POST','/api/v1/tasks',{'idempotency_key':str(uuid.uuid4()),'steps':[
        {'capability':'reminder.create@v1','arguments':{'title':'已经保存的提醒'}},
        {'capability':'web.search@v1','arguments':{'query':'Python docs'}}]})
    assert created.status_code==202,created.text
    tid=created.json()['id']
    await run_task(system[0].state,tid,alice.user_id)
    assert alice.request('GET','/api/v1/tasks/'+tid).json()['result']['status']=='stored'
    def transport(request):
        with system[2]() as db:
            scope(db,alice.user_id,'h1')
            if interrupt=='revoked':db.get(Device,alice.device_id).revoked=True
            else:db.get(Task,tid).deadline=now()-1
            db.commit()
        return httpx.Response(200,json={'results':[],'unresponsive_engines':failures()})
    system[0].state.registry=Registry(system[0].state.vault,httpx.MockTransport(transport))
    await run_task(system[0].state,tid,alice.user_id)
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,tid)
        rows=list(db.scalars(select(Invocation).where(Invocation.task_id==tid).order_by(Invocation.step)))
        assert rows[0].status=='SUCCEEDED'
        if interrupt=='revoked':
            assert task.status=='CANCELED' and task.result is None
            assert rows[1].status=='CANCELED' and rows[1].result is None
        else:
            assert task.status=='FAILED' and task.deadline<=now()
            assert rows[1].status=='UNAVAILABLE' and task.result is not None
    assert reminder_count(system,alice)==1
