"""隐私暂停恢复沿用同一次规划，不消费临时控制位或重复执行已完成步骤。"""
import json
import uuid
import httpx
import pytest
from sqlalchemy import select
from homeai import cloud_gateway
from homeai.cloud_gateway import PrivacyUnavailable
from homeai.crypto import canonical, digest
from homeai.db import Invocation, Task, scope, uid
from homeai.runtime import run_task
from test_planning_resumption import setup_models
from test_conversations import new_chat, send


@pytest.mark.asyncio
async def test_invalid_routing_reports_actual_stage_without_force_snapshot(system, alice):
    setup_models(system, alice)
    registry = system[0].state.registry
    original = registry.transport
    calls = []
    def invalid_route(request):
        calls.append(request.url.host)
        if request.url.host == 'fast.test':
            return httpx.Response(200, json={'choices': [{'message': {'content': '{"simple":"maybe"}'}}]})
        return original.handle_request(request)
    registry.transport = httpx.MockTransport(invalid_route)
    task_id = send(alice, new_chat(alice), '公开测试问题').json()['task_id']
    await run_task(system[0].state, task_id, alice.user_id)
    result = alice.request('GET', '/api/v1/tasks/' + task_id).json()
    assert result['status'] == 'WAITING_PRIVACY'
    assert 'simple' in result['error']
    report = alice.request('GET', '/api/v1/tasks/' + task_id + '/privacy-review')
    assert report.status_code == 200, report.text
    assert report.json()['stage'] == 'routing'
    assert report.json()['code'] == 'routing_invalid_response'
    assert report.json()['review_id'] is None
    assert not report.json()['can_force'] and not report.json()['blocks']
    assert calls == ['fast.test']
    registry.transport = original
    assert alice.request('POST', '/api/v1/tasks/' + task_id + '/resume').status_code == 200
    await run_task(system[0].state, task_id, alice.user_id)
    assert alice.request('GET', '/api/v1/tasks/' + task_id).json()['status'] == 'SUCCEEDED'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['answer_existing', 'final_round'])
async def test_manual_force_preserves_planning_controls_and_completed_steps(system, alice, monkeypatch, mode):
    setup_models(system, alice)
    app = system[0].state
    detections = []
    async def uncertain(*args, **kwargs):
        detections.append('checked')
        raise PrivacyUnavailable('本地模型无法确定此次文本是否已脱敏', code='uncertain', overrideable=True)
    monkeypatch.setattr(cloud_gateway, 'local_check', uncertain)
    task_id = send(alice, new_chat(alice), '请汇总已取得的公开资料。').json()['task_id']
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, task_id)
        body = app.vault.open(task.request, alice.user_id + ':task:' + task_id)
        body['_planner_cloud'] = True
        if mode == 'answer_existing':
            body['_answer_from_existing'] = True
        else:
            arguments = {'query': 'Python documentation'}
            body['max_steps'] = 1
            body['steps'] = [{'capability': 'web.search@v1', 'arguments': arguments}]
            body['_agent_batches'] = [{'steps': [0], 'message': {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': 'completed-call', 'type': 'function', 'function': {'name': 'search_web', 'arguments': json.dumps(arguments)}}]}}]
            identifier = uid()
            db.add(Invocation(id=identifier, owner_id=alice.user_id, household_id='h1', task_id=task_id, step=0,
                capability='web.search@v1', status='SUCCEEDED', arguments_hash=digest(canonical(arguments)),
                arguments=app.vault.seal(arguments, alice.user_id + ':invocation:' + identifier),
                result=app.vault.seal({'results': [{'title': 'Python 文档', 'url': 'https://docs.python.org/', 'content': '公开参考'}],
                    'status': 'ok', 'content_trust': 'untrusted_web'}, alice.user_id + ':invocation-result:' + identifier)))
        task.request = app.vault.seal(body, alice.user_id + ':task:' + task_id)
        db.commit()
    await run_task(app, task_id, alice.user_id)
    report = alice.request('GET', '/api/v1/tasks/' + task_id + '/privacy-review').json()
    assert report['can_force'] and report['review_id']
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, task_id)
        body = app.vault.open(task.request, alice.user_id + ':task:' + task_id)
        assert body['_model_rounds'] == 0 and body['_model_token_charge'] == 0
        assert not body.get('_final_round_used')
        if mode == 'answer_existing':assert body['_answer_from_existing'] is True
    response = alice.request('POST', '/api/v1/tasks/' + task_id + '/privacy-review', {
        'review_id': report['review_id'], 'decision': 'force', 'idempotency_key': str(uuid.uuid4())})
    assert response.status_code == 200, response.text
    await run_task(app, task_id, alice.user_id)
    result = alice.request('GET', '/api/v1/tasks/' + task_id).json()
    assert result['status'] == 'SUCCEEDED', result
    assert detections == ['checked']
    steps = alice.request('GET', '/api/v1/tasks/' + task_id + '/steps').json()
    assert len(steps) == (1 if mode == 'final_round' else 0)
    assert all(row['status'] == 'SUCCEEDED' for row in steps)
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, task_id)
        body = app.vault.open(task.request, alice.user_id + ':task:' + task_id)
        assert body['_model_rounds'] == 1 and body['_cloud_calls'] == 1
        assert body['_privacy_review']['status'] == 'consumed'
        assert 'candidate' not in body['_privacy_review']
