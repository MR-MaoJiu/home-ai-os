"""健康追问回到账本取统计；不读真机、不发送真实健康资料。"""
import json
import uuid
import pytest

from homeai.crypto import canonical, digest
from homeai.db import Invocation, Task, scope, uid
from test_conversations import finish, new_chat, send
from test_pending_chat_context import captured, projected
from test_security_data import put, record


def saved_sleep_turn(system, alice):
    conversation = new_chat(alice)
    previous = send(alice, conversation, '分析我的睡眠').json()
    parameters = {'types':['sleep'],'start_at':'2026-01-01T00:00:00Z','end_at':'2026-01-02T00:00:00Z'}
    identifiers = []
    for index, (start, end, stage) in enumerate(((1, 2, 3), (2, 4, 4))):
        payload = {**parameters,'schema_version':2,'query_complete':True,'truncated':False,
            'capture_id':'isolated-sleep','chunk_index':index,'chunk_count':2,'status':'available',
            'samples':[{'type':'sleep','sample_id':str(index),
                'start_at':f'2026-01-01T0{start}:00:00Z','end_at':f'2026-01-01T0{end}:00:00Z',
                'value':stage,'unit':'category'}]}
        identifiers.append(put(alice, record(source_id=f'health-followup-{index}',kind='health.snapshot',payload=payload)))
    app = system[0].state
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        task = db.get(Task, previous['task_id'])
        body = app.vault.open(task.request,alice.user_id+':task:'+task.id)
        arguments = {'kind':'read_health','target_member_id':'self','purpose':'本次睡眠分析','parameters':parameters}
        body['steps'] = [{'capability':'client.request@v1','arguments':arguments}]
        body['_record_dependencies'] = dict.fromkeys(identifiers,1)
        task.request = app.vault.seal(body,alice.user_id+':task:'+task.id)
        row = Invocation(id=uid(),owner_id=alice.user_id,household_id='h1',task_id=task.id,step=0,
            capability='client.request@v1',status='SUCCEEDED',arguments_hash=digest(canonical(arguments)),arguments='')
        row.arguments = app.vault.seal(arguments,alice.user_id+':invocation:'+row.id)
        row.result = app.vault.seal({'status':'succeeded','records':[{'id':identifier,'version':1} for identifier in identifiers]},
            alice.user_id+':invocation-result:'+row.id)
        db.add(row); db.commit()
    finish(system,alice,task.id,'已收到睡眠数据。')
    return conversation, previous, identifiers


def test_health_followup_rehydrates_combined_summary_instead_of_only_old_answer(system,alice):
    conversation, _, identifiers = saved_sleep_turn(system,alice)
    current = send(alice,conversation,'刚才的睡眠数据，请继续分析').json()
    body = captured(system,alice,current)
    encoded = json.dumps(projected(system,alice,body),ensure_ascii=False)
    assert 'asleep_hours' in encoded and '3.0' in encoded
    assert 'stage_seconds' in encoded and '7200' in encoded
    assert 'sample_id' not in encoded
    assert body['_record_dependencies'] == dict.fromkeys(identifiers,1)
    assert not body.get('_cloud_consents')


def test_unrelated_message_does_not_inherit_health_statistics(system,alice):
    conversation, _, _ = saved_sleep_turn(system,alice)
    current = send(alice,conversation,'讲一个笑话').json()
    body = captured(system,alice,current)
    encoded = json.dumps(projected(system,alice,body),ensure_ascii=False)
    assert 'asleep_hours' not in encoded and 'stage_seconds' not in encoded
    assert not body['_record_dependencies']


def test_deleted_source_is_not_rehydrated_from_old_receipt(system,alice):
    conversation, _, identifiers = saved_sleep_turn(system,alice)
    assert alice.request('DELETE','/api/v1/data/'+identifiers[0]).status_code == 200
    current = send(alice,conversation,'刚才的睡眠数据，请继续分析').json()
    encoded = json.dumps(projected(system,alice,captured(system,alice,current)),ensure_ascii=False)
    assert 'asleep_hours' not in encoded and 'stage_seconds' not in encoded


@pytest.mark.parametrize('message, expected', [
    ('再看一下我的睡眠怎么样', ['sleep']),
    ('我要你整理我的健康数据啊', []),
    ('跟我出一个健康报告，根据我的睡眠心率等等数据', ['sleep','heart_rate']),
    ('分析我的静息心率和心率变异性', ['resting_heart_rate','heart_rate_variability']),
    ('告诉我睡眠是什么', None),
    ('健康饮食有什么建议', None),
    ('帮我分析爸爸的健康报告', None),
    ('讲一个笑话', None),
])
def test_only_current_personal_health_purpose_discovers_saved_inventory(message,expected):
    from homeai.planner import personal_health_types
    assert personal_health_types(message) == expected


@pytest.mark.asyncio
async def test_new_health_report_can_discover_saved_data_without_raw_samples(system,alice):
    from homeai.runtime import run_task
    from test_task_clarification import setup_planner, tool
    saved_sleep_turn(system,alice)
    parameters = {'types':['sleep'],'start_at':'2026-01-01T00:00:00Z','end_at':'2026-01-02T00:00:00Z'}
    calls, _ = setup_planner(system,[{'tool_calls':[tool('request_client_data',{
        'kind':'read_health','target_member_id':'self','purpose':'根据已提供资料整理睡眠',
        'parameters':parameters})]}])
    created = alice.request('POST','/api/v1/tasks',{'idempotency_key':str(uuid.uuid4()),
        'message':'我要你整理我的健康数据啊','max_model_tokens':262144})
    assert created.status_code == 202
    await run_task(system[0].state,created.json()['id'],alice.user_id)
    encoded = json.dumps(calls[0]['messages'],ensure_ascii=False)
    assert 'saved_health_inventory' in encoded and '2026-01-01T00:00:00Z' in encoded
    assert 'sample_id' not in encoded and 'asleep_seconds' not in encoded


@pytest.mark.asyncio
async def test_followup_can_answer_from_rehydrated_health_without_recollecting(system,alice):
    from homeai.runtime import run_task
    from test_task_clarification import setup_planner
    conversation, _, _ = saved_sleep_turn(system,alice)
    setup_planner(system,[{'content':'已保存的这段睡眠合计3小时，深睡2小时。'}])
    current = send(alice,conversation,'刚才的睡眠数据，请继续分析').json()
    await run_task(system[0].state,current['task_id'],alice.user_id)
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        task = db.get(Task,current['task_id'])
        assert task.status == 'SUCCEEDED'
        body = system[0].state.vault.open(task.request,alice.user_id+':task:'+task.id)
        assert body['steps'] == [] and body['_health_history_evidence']


@pytest.mark.parametrize('message', ['根据刚才的睡眠创建提醒','分析睡眠并发送报告','分析睡眠并导出文件','重新采集最新睡眠数据'])
def test_health_evidence_does_not_substitute_new_required_tool_execution(message):
    from homeai.history_scope import health_followup_answerable
    assert not health_followup_answerable(message)
