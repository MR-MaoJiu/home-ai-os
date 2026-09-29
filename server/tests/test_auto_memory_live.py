"""隔离 PostgreSQL 验证真实聊天、候选确认、遗忘钩子与 RLS，不调用模型。"""
import os
import uuid
import pytest
from sqlalchemy import select,text
from fastapi.testclient import TestClient
from homeai.api import create_app
from homeai.config import Settings
from homeai.db import Record,scope
from homeai.auto_memory import MemoryLearning,MemoryForgetSource,MemoryForgetTombstone,forget_source_turns
from homeai.security import Actor
from conftest import SignedClient

pytestmark=pytest.mark.skipif(os.environ.get('HOMEAI_INTEGRATION')!='1',reason='需要已迁移隔离PostgreSQL测试库')


def chat(user,content):
    cid=user.request('POST','/api/v1/conversations',{'client_id':str(uuid.uuid4())}).json()['id']
    result=user.request('POST','/api/v1/conversations/'+cid+'/messages',{'client_key':str(uuid.uuid4()),'content':content})
    assert result.status_code==202,result.text
    return result.json()


def test_chat_memory_hooks_and_forget_rls(tmp_path):
    settings=Settings();settings.database_url=settings.database_url.rsplit('/',1)[0]+'/homeai_test';settings.state_dir=tmp_path
    app=create_app(settings);client=TestClient(app);household=str(uuid.uuid4())
    alice=SignedClient(client,app.state.db,household=household);bob=SignedClient(client,app.state.db,household=household)
    try:
        first=chat(alice,'我喜欢游泳')
        entries=alice.request('GET','/api/v1/memory/entries').json()
        assert len(entries)==1 and entries[0]['payload']['source_quote']=='我喜欢游泳'
        rid=entries[0]['id']
        assert bob.request('GET','/api/v1/memory/entries').json()==[]
        assert alice.request('DELETE','/api/v1/data/'+rid).status_code==200
        with app.state.db() as db:
            actor=Actor(alice.user_id,household,alice.device_id,'adult')
            assert first['id'] in forget_source_turns(db,actor)
            scope(db,bob.user_id,household)
            assert not db.scalar(select(MemoryForgetSource.id).where(MemoryForgetSource.owner_id==alice.user_id))
            assert not db.scalar(select(MemoryLearning.id).where(MemoryLearning.owner_id==alice.user_id))
            assert not db.scalar(select(MemoryForgetTombstone.id).where(MemoryForgetTombstone.owner_id==alice.user_id))
            rows=db.execute(text("SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname IN ('client_actions','task_data_grants','memory_learning','memory_forget_tombstones','memory_forget_sources')")).all()
            assert len(rows)==5 and all(enabled and forced for _,enabled,forced in rows)
        second=chat(alice,'我喜欢游泳。')
        assert alice.request('GET','/api/v1/memory/entries').json()==[]
        sensitive=chat(alice,'我的邮箱是test@example.com')
        candidates=alice.request('GET','/api/v1/memory/candidates').json()
        assert len(candidates)==1
        confirmed=alice.request('POST','/api/v1/memory/candidates/'+candidates[0]['id']+'/confirm')
        assert confirmed.status_code==200,confirmed.text
        record=alice.request('GET','/api/v1/data/'+confirmed.json()['record_id']).json()
        assert record['sensitivity']=='SENSITIVE'
        assert bob.request('GET','/api/v1/data/'+record['id']).status_code==404
        for response in (second,sensitive):alice.request('POST','/api/v1/tasks/'+response['task_id']+'/cancel')
    finally:
        client.close();app.state.db.kw['bind'].dispose()
