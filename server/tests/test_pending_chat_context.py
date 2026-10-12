"""收消息时固定上下文，晚到的任务结果和无关资料不串入新任务。"""
import json
import uuid
from types import SimpleNamespace

import pytest

from homeai.conversations import history
from homeai.db import Task, scope
from homeai.security import Actor
from test_conversations import finish, new_chat, send
from test_security_data import put, record


@pytest.mark.parametrize('message', ['把它改成英文', '这个可以简短点吗', '短一点', '换成中文'])
def test_natural_refinements_select_one_previous_turn(message):
    from homeai.history_scope import select_followup
    entries = [{'turn': SimpleNamespace(id='latest'), 'task': SimpleNamespace(id='task'), 'topics': set()}]
    assert select_followup({'message': message}, entries) == 'latest'


@pytest.mark.parametrize('message', ['你是谁', '给我介绍英语语法', '这个星期有几天'])
def test_new_questions_do_not_implicitly_reuse_private_history(message):
    from homeai.history_scope import select_followup
    entries = [{'turn': SimpleNamespace(id='private'), 'task': SimpleNamespace(id='task'), 'topics': {'health'}}]
    assert select_followup({'message': message}, entries) is None


def captured(system, alice, turn):
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, turn['task_id'])
        return system[0].state.vault.open(task.request, alice.user_id + ':task:' + task.id)


def projected(system, alice, body):
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        actor = Actor(alice.user_id, 'h1', alice.device_id, 'infrastructure_owner')
        return history(system[0].state, db, actor, body)


def test_pending_user_context_survives_late_completion_without_late_result(system, alice):
    conversation = new_chat(alice)
    previous = send(alice, conversation, '写一个青竹和海风的公开故事').json()
    current = send(alice, conversation, '刚才那个故事，先简短点').json()
    body = captured(system, alice, current)
    before = projected(system, alice, body)
    assert '青竹和海风' in before[0]['content']
    assert '尚未返回结果' in before[1]['content']
    finish(system, alice, previous['task_id'], '后完成的结果不能混入既定快照')
    assert projected(system, alice, body) == before
    assert '后完成的结果' not in json.dumps(before, ensure_ascii=False)
    # 下一条消息才允许看到在它创建前已经完成的结果。
    later = send(alice, conversation, '换一个话题').json()
    assert '后完成的结果' in json.dumps(projected(system, alice, captured(system, alice, later)), ensure_ascii=False)


def test_pending_sensitive_text_is_not_inherited_by_unrelated_question(system, alice):
    conversation = new_chat(alice)
    previous = send(alice, conversation, '请分析我的心率，敏感样例只用于本地测试').json()
    unrelated = send(alice, conversation, '太阳为什么发光').json()
    body = captured(system, alice, unrelated)
    assert projected(system, alice, body) == []
    assert not body['_history_requires_disclosure']
    followup = send(alice, conversation, '刚才的健康资料呢？').json()
    encoded = json.dumps(projected(system, alice, captured(system, alice, followup)), ensure_ascii=False)
    assert '敏感样例只用于本地测试' in encoded
    assert previous['task_id'] != followup['task_id']


def test_pending_snapshot_never_absorbs_tool_dependencies_acquired_later(system, alice):
    conversation = new_chat(alice)
    previous = send(alice, conversation, '处理公开测试资料').json()
    followup = send(alice, conversation, '刚才那项先说说进度').json()
    body = captured(system, alice, followup)
    source = put(alice, record(source_id='late-tool-result', kind='health.steps', payload={'text': '晚到样本'}))
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, previous['task_id'])
        old = system[0].state.vault.open(task.request, alice.user_id + ':task:' + task.id)
        old['_record_dependencies'] = {source: 1}
        task.request = system[0].state.vault.seal(old, alice.user_id + ':task:' + task.id)
        db.commit()
    messages = projected(system, alice, body)
    assert '处理公开测试资料' in json.dumps(messages, ensure_ascii=False)
    assert body['_record_dependencies'] == {}
    assert source not in body['_historical_record_ids']


def test_public_tool_dependency_does_not_leak_into_unrelated_memory_request(system, alice):
    conversation = new_chat(alice)
    source_body = record(source_id='public-note', kind='note', payload={'text': '公开来源标记'})
    source_body.update(sensitivity='PUBLIC', cloud_policy='REDACT_AND_ALLOW')
    source = put(alice, source_body)
    previous = send(alice, conversation, '查询公开说明').json()
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, previous['task_id'])
        old = system[0].state.vault.open(task.request, alice.user_id + ':task:' + task.id)
        old['_record_dependencies'] = {source: 1}
        task.request = system[0].state.vault.seal(old, alice.user_id + ':task:' + task.id)
        db.commit()
    finish(system, alice, previous['task_id'], '公开来源标记和答案')
    current = send(alice, conversation, '以后回答短一点').json()
    body = captured(system, alice, current)
    encoded = json.dumps(projected(system, alice, body), ensure_ascii=False)
    assert '公开来源标记' not in encoded
    assert body['_record_dependencies'] == {}
    linked = send(alice, conversation, '刚才的公开说明再解释一下').json()
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, linked['task_id'])
        value = system[0].state.vault.open(task.request, alice.user_id + ':task:' + task.id)
        value['_reply_to_task_id'] = previous['task_id']
    encoded = json.dumps(projected(system, alice, value), ensure_ascii=False)
    assert '公开来源标记' in encoded
    assert value['_record_dependencies'] == {source: 1}


def test_progress_ignores_chat_and_finds_explicit_task_older_than_five_turns(system, alice):
    conversation = new_chat(alice)
    previous = send(alice, conversation, '读取服务器CPU').json()
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        db.get(Task, previous['task_id']).status = 'EXECUTING'
        db.commit()
    for index in range(7):
        ordinary = send(alice, conversation, f'普通闲聊第{index}轮').json()
        finish(system, alice, ordinary['task_id'], '普通回答')
    progress = alice.request('POST', '/api/v1/conversations/' + conversation + '/messages', {
        'client_key': str(uuid.uuid4()), 'content': '任务进度', 'reply_to_task_id': previous['task_id']})
    assert progress.status_code == 202
    answer = progress.json()['assistant_text']
    assert '执行中' in answer and 'CPU' in answer
    assert '普通闲聊' not in answer and '已完成' not in answer


def test_progress_never_lists_ordinary_answers_as_completed_tasks(system, alice):
    conversation = new_chat(alice)
    ordinary = send(alice, conversation, '你好').json()
    finish(system, alice, ordinary['task_id'], '你好呀')
    assert send(alice, conversation, '任务进度').json()['assistant_text'] == '现在没有正在处理的任务。'


def test_chat_projection_revision_advances_for_result_and_revocation(system, alice):
    from homeai.data import emit
    conversation = new_chat(alice)
    original = send(alice, conversation, '公开消息版本测试').json()
    source = put(alice, record(source_id='revision-source', kind='note', payload={'text': '来源内容'}))
    actor = Actor(alice.user_id, 'h1', alice.device_id, 'infrastructure_owner')
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, original['task_id'])
        payload = system[0].state.vault.open(task.request, alice.user_id + ':task:' + task.id)
        payload['_record_dependencies'] = {source: 1}
        task.request = system[0].state.vault.seal(payload, alice.user_id + ':task:' + task.id)
        task.result = system[0].state.vault.seal({'text': '完成后的版本'}, alice.user_id + ':task-result:' + task.id)
        task.status = 'SUCCEEDED'
        emit(db, actor, 'task.updated', task.id)
        db.commit()
    done = alice.request('GET', '/api/v1/conversations/' + conversation).json()['turns'][0]
    assert done['display_revision'] > original['display_revision']
    assert done['assistant_text'] == '完成后的版本'
    assert alice.request('DELETE', '/api/v1/data/' + source).status_code == 200
    revoked = alice.request('GET', '/api/v1/conversations/' + conversation).json()['turns'][0]
    assert revoked['display_revision'] > done['display_revision']
    assert '已隐藏' in revoked['assistant_text']
    # 相同状态的重复读取保持版本，不用到达顺序猜测消息的新旧。
    again = alice.request('GET', '/api/v1/conversations/' + conversation).json()['turns'][0]
    assert again['display_revision'] == revoked['display_revision']


def test_legacy_failed_tool_list_does_not_break_entire_chat_page(system, alice):
    conversation = new_chat(alice)
    turn = send(alice, conversation, '公开旧格式结果测试').json()
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, turn['task_id'])
        task.result = system[0].state.vault.seal([{'text': '工具内部结果不直接展示'}], alice.user_id + ':task-result:' + task.id)
        task.status, task.error = 'FAILED', '旧任务收尾未完成'
        db.commit()
    response = alice.request('GET', '/api/v1/conversations/' + conversation)
    assert response.status_code == 200
    item = response.json()['turns'][0]
    assert item['assistant_parts'] == [] and item['sources'] == []
    assert '没办成' in item['assistant_text']
