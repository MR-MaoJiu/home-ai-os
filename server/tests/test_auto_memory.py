"""有限偏好语法、候选边界、来源证据与遗忘重放的临时数据库验证。"""
import uuid
import pytest
from fastapi import HTTPException
from sqlalchemy import select,delete,func
from conftest import SignedClient
from homeai.db import Conversation,ConversationTurn,MemoryCandidate,Record,scope
from homeai.security import Actor
from homeai.auto_memory import (MemoryLearning,MemoryForgetTombstone,observe_turn,parse_statement,
    candidate_sensitivity,before_confirm,remember_forget,replay_forget_journal)


def actor(client):return Actor(client.user_id,'h1',client.device_id,'adult')


def turn(system,user,content):
    app=system[0].state;cid=str(uuid.uuid4());tid=str(uuid.uuid4())
    with system[2]() as db:
        scope(db,user.user_id,'h1')
        db.add(Conversation(id=cid,owner_id=user.user_id,household_id='h1',title=app.vault.seal('临时测试',user.user_id+':conversation:'+cid),next_sequence=1))
        db.add(ConversationTurn(id=tid,owner_id=user.user_id,household_id='h1',conversation_id=cid,sequence=1,client_key=str(uuid.uuid4()),
            request_hash='test-only',user_message=app.vault.seal(content,user.user_id+':chat-turn:'+tid),task_id=str(uuid.uuid4())))
        db.commit()
    return tid


def learn(system,user,content,payload=None,turn_id=None):
    identifier=turn_id or turn(system,user,content)
    with system[2]() as db:
        scope(db,user.user_id,'h1');source=db.get(ConversationTurn,identifier)
        result=observe_turn(system[0].state,db,actor(user),source,payload or {})
        db.commit();return result


def test_literal_own_preference_is_saved_with_exact_provenance_and_deduplicated(system,alice):
    text='我喜欢喝茶。';tid=turn(system,alice,text)
    result=learn(system,alice,text,turn_id=tid)
    assert result['status']=='SAVED'
    again=learn(system,alice,text,turn_id=tid)
    assert again['record_id']==result['record_id']
    assert learn(system,alice,'我喜欢喝茶')['status']=='DUPLICATE'
    record=alice.request('GET','/api/v1/data/'+result['record_id']).json()
    assert record['source']=='conversation_memory' and record['visibility']=='personal'
    assert record['payload']['source_quote']==text and record['payload']['turn_id']==tid
    assert record['payload']['extraction_method']=='literal_preference_v1'
    bob=SignedClient(system[1],system[2])
    assert bob.request('GET','/api/v1/data/'+result['record_id']).status_code==404
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        assert db.scalar(select(func.count()).select_from(Record).where(Record.owner_id==alice.user_id,Record.kind=='memory.fact'))==1


def test_unknown_quoted_or_tainted_content_is_never_automatically_learned(system,alice):
    for text in ['他说：“我喜欢喝茶”','如果我喜欢喝茶呢？','例如我喜欢喝茶','我喜欢某种未支持的新活动','password: secret-test-value']:
        assert parse_statement(text) is None
    for payload in [{'_mentions':[{'member_id':'someone'}]},{'_task_grants':{'x':'y'}},{'record_ids':['x']},{'_record_dependencies':{'x':1}},{'parts':[{'type':'image'}]}]:
        assert learn(system,alice,'我喜欢跑步',payload)['status']=='IGNORED'
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        assert db.scalar(select(func.count()).select_from(Record).where(Record.owner_id==alice.user_id))==0


def test_sensitive_statement_is_only_a_candidate_and_preserves_sensitive_classification(system,alice):
    assert parse_statement('我的邮箱是test@example.com')['classification']=='sensitive'
    assert parse_statement('我有过敏@另一成员') is None
    result=learn(system,alice,'我有花生过敏。')
    assert result['status']=='CANDIDATE' and result['reason']=='sensitive'
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        candidate=db.get(MemoryCandidate,result['candidate_id'])
        assert candidate.status=='PENDING'
        assert candidate_sensitivity(db,candidate.id)=='SENSITIVE'
        assert db.scalar(select(func.count()).select_from(Record).where(Record.owner_id==alice.user_id,Record.kind=='memory.fact'))==0


def test_conflict_waits_for_user_without_overwriting_old_memory(system,alice):
    old=learn(system,alice,'我喜欢喝茶')
    conflict=learn(system,alice,'我不喜欢喝茶')
    assert conflict['status']=='CANDIDATE' and conflict['reason']=='conflict'
    assert alice.request('GET','/api/v1/data/'+old['record_id']).json()['payload']['content']=='我喜欢喝茶'
    with system[2]() as db:
        scope(db,alice.user_id,'h1');candidate=db.get(MemoryCandidate,conflict['candidate_id'])
        with pytest.raises(HTTPException) as error:before_confirm(system[0].state,db,actor(alice),candidate)
        assert error.value.status_code==409
        record=db.get(Record,old['record_id']);remember_forget(system[0].state,db,actor(alice),record)
        record.deleted=True;record.payload='';db.flush()
        before_confirm(system[0].state,db,actor(alice),candidate)
        db.commit()


def test_forgotten_preference_does_not_revive_after_replay_or_new_turn(system,alice):
    result=learn(system,alice,'我喜欢游泳')
    with system[2]() as db:
        scope(db,alice.user_id,'h1');record=db.get(Record,result['record_id'])
        remember_forget(system[0].state,db,actor(alice),record)
        record.deleted=True;record.payload='';db.commit()
    assert learn(system,alice,'我喜欢游泳。')['status']=='FORGOTTEN'
    journal=system[0].state.settings.state_dir/'memory-forget.jsonl'
    assert journal.stat().st_mode&0o777==0o600 and '游泳' not in journal.read_text()
    # 旧备份可能没有新墓碑；独立遗忘日志仍阻止重新从旧聊天抽取。
    with system[2]() as db:
        scope(db,alice.user_id,'h1');db.execute(delete(MemoryForgetTombstone));db.execute(delete(MemoryLearning));db.commit()
    assert learn(system,alice,'我喜欢游泳')['status']=='FORGOTTEN'
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        assert db.scalar(select(func.count()).select_from(Record).where(Record.owner_id==alice.user_id,Record.deleted.is_(False)))==0


def test_bad_forget_journal_fails_closed_without_blocking_normal_chat(system,alice):
    journal=system[0].state.settings.state_dir/'memory-forget.jsonl';journal.write_text('not encrypted')
    assert learn(system,alice,'我喜欢摄影')['status']=='UNAVAILABLE'


def test_forget_source_turns_survive_independent_journal_replay(system,alice):
    from homeai.auto_memory import MemoryForgetSource,forget_source_turns
    text='我喜欢读书';tid=turn(system,alice,text)
    result=learn(system,alice,text,turn_id=tid)
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        remember_forget(system[0].state,db,actor(alice),db.get(Record,result['record_id']));db.commit()
        assert tid in forget_source_turns(db,actor(alice))
        db.execute(delete(MemoryForgetSource));db.execute(delete(MemoryForgetTombstone));db.commit()
        replay_forget_journal(db,system[0].state.vault,system[0].state.settings.state_dir/'memory-forget.jsonl')
        db.commit();assert tid in forget_source_turns(db,actor(alice))
