"""隔离回归：聊天原话、称呼方向、混合请求和旧来源的记忆边界。"""
import pytest
from homeai.auto_memory import memory_message
from homeai.assistant_persona import preferences
from homeai.db import ConversationTurn, Task, scope
from homeai.memory import create_chat_candidate, save_chat_candidate
from homeai.security import Actor
from test_auto_memory import learn
from test_conversations import new_chat, send
from test_security_data import put, record


def entries(alice):
    return alice.request('GET', '/api/v1/memory/entries').json()


def person(alice):
    return Actor(alice.user_id, 'h1', alice.device_id, 'infrastructure_owner')


def test_addressing_preserves_who_is_assistant_and_who_is_member(system, alice):
    first = learn(system, alice, '你叫小竹，我叫小林。')
    assert first['status'] == 'SAVED'
    assert entries(alice)[0]['payload']['addressing'] == {'assistant_name': '小竹', 'preferred_name': '小林'}
    updated = learn(system, alice, '以后叫我小松。')
    assert updated['status'] == 'SAVED' and updated['record_id'] == first['record_id']
    assert len(entries(alice)) == 1 and entries(alice)[0]['version'] == 2
    with system[2]() as db:
        assert preferences(system[0].state, db, person(alice)) == {'assistant_name': '小竹', 'preferred_name': '小松'}
    assert learn(system, alice, '请叫我小松')['status'] == 'DUPLICATE'


@pytest.mark.parametrize('message', ['你叫我打开设置', '你叫外卖', '你叫我输入密码', '如果我叫小林呢？'])
def test_actions_and_hypotheticals_are_not_personal_names(message):
    assert memory_message(message) is None


@pytest.mark.parametrize('message', ['记住我喜欢喝茶，帮我查附近茶馆', '我喜欢喝茶，然后帮我查附近茶馆'])
def test_memory_and_task_in_same_message_are_both_preserved(system, alice, message):
    response = send(alice, new_chat(alice), message).json()
    saved = entries(alice)
    assert len(saved) == 1 and '附近茶馆' not in saved[0]['payload']['content']
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, response['task_id'])
        body = system[0].state.vault.open(task.request, alice.user_id + ':task:' + task.id)
        assert task.status == 'RECEIVED'
        assert body['_memory_observation']['handled_only'] is False
        assert body['message'].endswith('帮我查附近茶馆')
        turn = db.get(ConversationTurn, response['id'])
        assert system[0].state.vault.open(turn.user_message, alice.user_id + ':chat-turn:' + turn.id) == message


def test_followup_save_uses_user_statement_not_assistant_claim(system, alice):
    conversation = new_chat(alice)
    original = send(alice, conversation, '你的名字是小竹，请叫我小林').json()
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        previous = db.get(Task, original['task_id'])
        previous.result = system[0].state.vault.seal({'text': '颠倒的测试回答：你叫小竹，我叫小林'}, alice.user_id + ':task-result:' + previous.id)
        db.commit()
    followup = send(alice, conversation, '提交').json()
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, followup['task_id'])
        body = system[0].state.vault.open(task.request, alice.user_id + ':task:' + task.id)
        assert body['_memory_observation']['status'] == 'DUPLICATE'
        assert preferences(system[0].state, db, person(alice)) == {'assistant_name': '小竹', 'preferred_name': '小林'}


def test_tool_memory_ignores_unrelated_historical_document_but_uses_current_quote(system, alice):
    conversation = new_chat(alice)
    result = send(alice, conversation, '记住你叫小竹，我叫小林').json()
    document = put(alice, record(kind='document.text', payload={'content': '文件里的另一个称呼，不是用户偏好'}))
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, result['task_id'])
        body = system[0].state.vault.open(task.request, alice.user_id + ':task:' + task.id)
        body.update(record_ids=[document], _historical_record_ids=[document], _record_dependencies={document: 1})
        task.request = system[0].state.vault.seal(body, alice.user_id + ':task:' + task.id)
        candidate = create_chat_candidate(system[0].state, db, person(alice), conversation, result['id'], '你叫小林，我叫小竹')
        memory = save_chat_candidate(system[0].state, db, person(alice), candidate, require_current_source=True)
        payload = system[0].state.vault.open(memory.payload, alice.user_id + ':record:' + memory.id)
        assert payload['addressing'] == {'assistant_name': '小竹', 'preferred_name': '小林'}
        assert candidate.status == 'CONFIRMED'
        db.commit()
    assert len(entries(alice)) == 1


def test_forgotten_source_cannot_reappear_but_new_explicit_statement_can_be_saved(system, alice):
    conversation = new_chat(alice)
    original = send(alice, conversation, '请记住我喜欢喝茶').json()
    identifier = entries(alice)[0]['id']
    assert alice.request('DELETE', '/api/v1/memory/entries/' + identifier).status_code == 200
    followup = send(alice, conversation, '记住').json()
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, followup['task_id'])
        body = system[0].state.vault.open(task.request, alice.user_id + ':task:' + task.id)
        assert body['_memory_observation']['status'] == 'FORGOTTEN'
    assert entries(alice) == []
    assert learn(system, alice, '请记住我喜欢喝茶')['status'] == 'SAVED'
    assert len(entries(alice)) == 1


@pytest.mark.parametrize('message', [
    '以后我叫你小竹，你可以叫我小林就行。',
    '你的名字叫小竹，请叫我小林，记住。',
    '明明是我叫你小竹你叫我小林',
])
def test_natural_addressing_keeps_both_roles(message):
    assert memory_message(message)['statement']['addressing'] == {'assistant_name': '小竹', 'preferred_name': '小林'}


def test_repeated_submit_reaches_only_same_personal_declaration(system, alice):
    conversation = new_chat(alice)
    send(alice, conversation, '以后我叫你小竹，你叫我小林')
    send(alice, conversation, '提交')
    result = send(alice, conversation, '重新提交').json()
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, result['task_id'])
        body = system[0].state.vault.open(task.request, alice.user_id + ':task:' + task.id)
        assert body['_memory_observation']['status'] == 'DUPLICATE'
    send(alice, conversation, '另一件事，今天星期几？')
    result = send(alice, conversation, '重新提交').json()
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, result['task_id'])
        body = system[0].state.vault.open(task.request, alice.user_id + ':task:' + task.id)
        assert body['_memory_observation']['status'] == 'IGNORED'
    assert len(entries(alice)) == 1



def test_corrected_addressing_followup_does_not_repeat_reversed_assistant_answer(system, alice):
    conversation = new_chat(alice)
    original = send(alice, conversation, '明明是我叫你小竹你叫我小林').json()
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, original['task_id'])
        task.result = system[0].state.vault.seal({'text': '错误测试回答：你是小竹，我是小林。'}, alice.user_id + ':task-result:' + task.id)
        db.commit()
    followup = send(alice, conversation, '重新提交').json()
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        task = db.get(Task, followup['task_id'])
        body = system[0].state.vault.open(task.request, alice.user_id + ':task:' + task.id)
        assert body['_memory_observation']['status'] == 'DUPLICATE'
        candidate = create_chat_candidate(system[0].state, db, person(alice), conversation, followup['id'], '你叫小林，我叫小竹')
        saved = save_chat_candidate(system[0].state, db, person(alice), candidate, require_current_source=True)
        payload = system[0].state.vault.open(saved.payload, alice.user_id + ':record:' + saved.id)
        assert payload['addressing'] == {'assistant_name': '小竹', 'preferred_name': '小林'}
        assert preferences(system[0].state, db, person(alice)) == {'assistant_name': '小竹', 'preferred_name': '小林'}
