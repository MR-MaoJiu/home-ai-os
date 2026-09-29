"""原文摘抄保真只使用当前授权来源，不调用模型或真实网络。"""
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from conftest import SignedClient
from homeai.db import Record,scope
from homeai.security import Actor
from homeai.media import process_user
from homeai.source_fidelity import clean_answer,faithful_answer
from test_media import upload


async def source(system,user,marker):
    response=upload(user,(marker+'\n这是一条用于原文摘抄的公开测试资料。').encode(),'verbatim.txt')
    assert response.status_code==200,response.text
    identifier=response.json()['asset']['record_id']
    await process_user(system[0].state,user.user_id,'h1')
    with system[2]() as db:
        scope(db,user.user_id,'h1')
        child=db.scalar(select(Record).where(Record.owner_id==user.user_id,Record.source=='document_parse',Record.source_id==identifier))
        assert child is not None
        return identifier,child.id


def body(source_id,child_id):
    return {'message':'读取文件首行，按原文只回复该编号。','record_ids':[source_id],
        '_record_dependencies':{source_id:1,child_id:1},
        '_derived_task_sources':{child_id:{'source_id':source_id,'source_version':1,'version':1}}}


@pytest.mark.asyncio
async def test_short_literal_preserves_authorized_original_case_and_removes_reasoning_marker(system,alice):
    original='AbC-TEST-90';source_id,child=await source(system,alice,original)
    actor=Actor(alice.user_id,'h1',alice.device_id,'adult')
    with system[2]() as db:
        assert faithful_answer(system[0].state,db,actor,body(source_id,child),'</think>\n\nabc-test-90')==original
        assert clean_answer('<think>这部分不应该展示</think>\n答案')=='答案'
        assert faithful_answer(system[0].state,db,actor,{**body(source_id,child),'message':'概括资料内容'},'</think>\nabc-test-90')=='abc-test-90'


@pytest.mark.asyncio
async def test_revoked_permission_or_changed_source_version_cannot_correct_using_stale_source(system,alice):
    bob=SignedClient(system[1],system[2]);source_id,child=await source(system,bob,'Source-CASE-22')
    assert bob.request('PUT','/api/v1/data/'+source_id+'/visibility',{'visibility':'family'}).status_code==200
    reader=Actor(alice.user_id,'h1',alice.device_id,'adult');payload=body(source_id,child)
    with system[2]() as db:
        assert faithful_answer(system[0].state,db,reader,payload,'source-case-22')=='Source-CASE-22'
    assert bob.request('PUT','/api/v1/data/'+source_id+'/visibility',{'visibility':'personal'}).status_code==200
    with system[2]() as db:
        with pytest.raises(HTTPException):faithful_answer(system[0].state,db,reader,payload,'source-case-22')
        scope(db,bob.user_id,'h1');db.get(Record,source_id).version=2;db.commit()
        owner=Actor(bob.user_id,'h1',bob.device_id,'adult')
        with pytest.raises(HTTPException):faithful_answer(system[0].state,db,owner,payload,'source-case-22')
