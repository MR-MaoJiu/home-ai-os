"""风险由 Core 的能力和执行范围决定，模型不能自报为低风险。"""
import json
import httpx
import pytest
from fastapi import HTTPException
from homeai.policy import Policy, capability_risk
from homeai.security import Actor


@pytest.mark.parametrize('targets',[['cpu'],['memory'],['cpu','memory']])
def test_fixed_performance_metrics_are_low_risk(targets):
    assert capability_risk('host.inspect@v1',{'targets':targets,'purpose':'诊断性能'})==1


@pytest.mark.parametrize('arguments',[
    None,{}, {'targets':[]}, {'targets':'cpu'}, {'targets':['disk']},
    {'targets':['cpu','screenshot']}, {'targets':['screenshot']},
    {'targets':['cpu',None]},
])
def test_unknown_or_sensitive_inspection_scope_keeps_approval(arguments):
    assert capability_risk('host.inspect@v1',arguments)==3


@pytest.mark.parametrize('capability',[
    'host.execute@v1','mail.send@v1','member.notify@v1','home.execute@v1',
])
def test_model_readonly_claim_cannot_downgrade_dangerous_capability(capability):
    assert capability_risk(capability,{'risk':0,'approved':True,'read_only':True,'impact_summary':'只读，无风险'})==3


@pytest.mark.parametrize('capability',[
    'shell.run@v1','web.search@v1','knowledge.search@v1','resource.fetch@v1',
    'resource.script@v1','resource.send@v1','image.generate@v1',
    'automation.create@v1','automation.stop@v1','reminder.create@v1',
])
def test_routine_capabilities_do_not_need_per_step_approval(capability):
    assert capability_risk(capability)<3


async def test_opa_receives_core_computed_risk_and_can_still_deny():
    inputs=[]
    def request(value):
        inputs.append(json.loads(value.content)['input'])
        return httpx.Response(200,json={'result':len(inputs)==1})
    policy=Policy('http://opa',httpx.MockTransport(request))
    actor=Actor('member','family','device','adult')
    await policy.check(actor,'host.inspect@v1',arguments={'targets':['cpu','memory'],'purpose':'诊断'})
    assert inputs[0]['risk']==1 and inputs[0]['approved'] is False
    with pytest.raises(HTTPException,match='策略拒绝执行'):
        await policy.check(actor,'shell.run@v1')
    assert inputs[1]['risk']==2


async def test_missing_scope_never_silently_bypasses_inspection_approval():
    def unexpected(value):raise AssertionError('审批缺失时不能送入执行策略')
    policy=Policy('http://opa',httpx.MockTransport(unexpected))
    actor=Actor('member','family','device','adult')
    for arguments in (None,{'targets':['screenshot'],'risk':0,'approved':True}):
        with pytest.raises(HTTPException,match='有效审批'):
            await policy.check(actor,'host.inspect@v1',arguments=arguments)
