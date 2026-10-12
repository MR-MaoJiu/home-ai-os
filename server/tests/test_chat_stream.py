import json
import httpx
import pytest
from homeai.model_stream import generate,visible_text
from homeai.chat_stream import publisher
from homeai.db import Task,Device,scope
from homeai.security import Actor
from test_conversations import new_chat,send

@pytest.mark.parametrize('raw,expected',[
 ('<thi',''),('<think>隐藏过程',''),('<think>隐藏过程</think>你好','你好'),
 ('</think>\n你好','你好'),('你好<thi','你好'),('正常回答','正常回答')])
def test_reasoning_is_never_published(raw,expected):assert visible_text(raw)==expected

@pytest.mark.asyncio
async def test_real_sse_chunks_are_consumed_before_completion_and_usage_retained():
    observed=[]
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for content in ['你','好']:
                yield ('data: '+json.dumps({'choices':[{'delta':{'content':content}}]})+'\n\n').encode()
            assert observed==['你','你好']
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"total_tokens":4}}\n\ndata: [DONE]\n\n'
    async def collect(text):observed.append(text)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Stream()))) as client:
        result=await generate(client,'https://model.test',{}, {},collect)
    assert result['choices'][0]['message']['content']=='你好'
    assert result['usage']['total_tokens']==4

@pytest.mark.asyncio
async def test_partial_is_visible_only_to_owner_and_not_final_after_failure(system,alice):
    chat=new_chat(alice);turn=send(alice,chat,'公开流式样例').json();identifier=turn['task_id']
    with system[2]() as db:
        scope(db,alice.user_id,'h1');db.get(Task,identifier).status='EXECUTING';db.commit()
    publish=publisher(system[0].state,Actor(alice.user_id,'h1',alice.device_id,'infrastructure_owner'),identifier)
    await publish('第一段真实内容')
    result=alice.request('GET','/api/v1/conversations/'+chat).json()['turns'][0]
    assert result['assistant_text']=='第一段真实内容' and result['status']=='EXECUTING'
    assert result['requires_online_revalidation'] is True
    from conftest import SignedClient
    bob=SignedClient(system[1],system[2])
    assert bob.request('GET','/api/v1/conversations/'+chat).status_code==404
    with system[2]() as db:
        scope(db,alice.user_id,'h1');db.get(Task,identifier).status='FAILED';db.commit()
    assert '第一段' not in alice.request('GET','/api/v1/conversations/'+chat).json()['turns'][0]['assistant_text']
    import asyncio
    await asyncio.sleep(.26)
    with pytest.raises(Exception):await publish('失败后不能继续发布')

@pytest.mark.asyncio
async def test_live_local_model_publishes_before_task_finishes(system,alice):
    import os,asyncio,time
    if os.environ.get('HOMEAI_STREAM_LIVE')!='1':pytest.skip('需显式开启本地真实模型验收')
    from homeai.db import Provider
    from homeai.contracts import ProviderManifest
    from homeai.policy import Policy
    from homeai.config import Settings
    from homeai.runtime import run_task
    async with httpx.AsyncClient(trust_env=False) as client:
        response=await client.get('http://127.0.0.1:58100/v1/models');response.raise_for_status()
        model=response.json()['data'][0]['id']
    with system[2]() as db:
        manifest=ProviderManifest(id='local.stream.acceptance',version='1',adapter='openai',endpoint='http://127.0.0.1:58100/v1',model=model,allowed_hosts=['127.0.0.1'],capabilities={'model.generate@v1':'chat'})
        db.add(Provider(id=manifest.id,manifest=manifest.model_dump_json(),enabled=True));db.commit()
    app=system[0].state;app.policy=Policy(Settings().opa_url)
    identifier=new_chat(alice);turn=send(alice,identifier,'你好').json();task_id=turn['task_id']
    start=time.monotonic();first=None
    work=asyncio.create_task(run_task(app,task_id,alice.user_id))
    try:
        async with asyncio.timeout(60):
            while not work.done():
                with system[2]() as db:
                    scope(db,alice.user_id,'h1');task=db.get(Task,task_id)
                    if task.result:
                        value=app.vault.open(task.result,alice.user_id+':task-result:'+task_id)
                        if value.get('_streaming') and value.get('text') and first is None:first=time.monotonic()-start
                await asyncio.sleep(.01)
            await work
        with system[2]() as db:
            scope(db,alice.user_id,'h1');assert db.get(Task,task_id).status=='SUCCEEDED'
        assert first is not None,'必须在任务完成前读到真实增量'
        print('live_first_partial_seconds',round(first,3),'total_seconds',round(time.monotonic()-start,3))
    finally:
        if not work.done():work.cancel();await asyncio.gather(work,return_exceptions=True)
