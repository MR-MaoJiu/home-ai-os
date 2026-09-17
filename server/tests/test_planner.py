import pytest
from fastapi import HTTPException
from homeai.planner import decode_proposal


def proposal(name,args):return {'choices':[{'message':{'tool_calls':[{'function':{'name':name,'arguments':args}}]}}]}

def test_model_cannot_expand_tools_or_arguments():
    assert decode_proposal(proposal('create_reminder','{"title":"买牛奶"}'))==('reminder.create@v1',{'title':'买牛奶'})
    with pytest.raises(HTTPException):decode_proposal(proposal('shell','{"command":"x"}'))
    with pytest.raises(HTTPException):decode_proposal(proposal('create_reminder','{"title":"x","owner_id":"someone-else"}'))
    with pytest.raises(HTTPException):decode_proposal(proposal('create_reminder','{"title":{}}'))


def test_batch_tools_obey_remaining_budget():
    from homeai.planner import decode_proposals
    single = proposal('create_reminder', '{"title":"买牛奶"}')
    calls = single['choices'][0]['message']['tool_calls']
    calls.append({'function': {'name': 'search_calendar', 'arguments': '{"query":"明天"}'}})
    assert len(decode_proposals(single, max_calls=2)) == 2
    with pytest.raises(HTTPException):
        decode_proposals(single, max_calls=1)
    with pytest.raises(HTTPException):
        decode_proposals(single, max_calls=0)


@pytest.mark.asyncio
async def test_agent_token_budget_stops_before_model_request(system, alice):
    from homeai.runtime import run_task
    response = alice.request('POST', '/api/v1/tasks', {'message':'请创建提醒', 'idempotency_key':'agent-budget-00001', 'max_model_tokens':256})
    tid = response.json()['id']
    # 此环境没有模型 Provider；若错误地尝试调用模型会报配置缺失而非预算不足。
    await run_task(system[0].state, tid)
    result = alice.request('GET', '/api/v1/tasks/' + tid).json()
    assert result['status'] == 'FAILED'
    assert 'Token 预算不足' in result['error']
    assert result['execution']['model_rounds'] == 0
    assert alice.request('GET', '/api/v1/tasks/' + tid + '/steps').json() == []
