"""实际文本原件解析、临时任务授权及派生版本绑定；不调用模型。"""
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from homeai.db import Task,Record,scope
from homeai.media import process_user,context
from homeai.client_actions import read_authorized_record,task_documents_search,task_rehydrate
from homeai.security import Actor
from conftest import SignedClient
from test_media import upload
from test_client_actions import new_task,request_action,response


async def parsed_file(system,user):
    result=upload(user,b'This file contains the literal needle and its source evidence.','source.txt')
    assert result.status_code==200,result.text
    rid=result.json()['asset']['record_id']
    await process_user(system[0].state,user.user_id,'h1')
    with system[2]() as db:
        scope(db,user.user_id,'h1')
        child=db.scalar(select(Record).where(Record.source=='document_parse',Record.source_id==rid,Record.owner_id==user.user_id))
        assert child is not None
        return rid,child.id


def binding(source,child):
    return {'_derived_task_sources':{child:{'source_id':source,'version':1,'source_version':1}},'_record_dependencies':{source:1,child:1}}


@pytest.mark.asyncio
async def test_media_context_returns_actual_child_metadata_and_rejects_secret(system,alice):
    source,child=await parsed_file(system,alice)
    reader=Actor(alice.user_id,'h1',alice.device_id,'adult')
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        value=context(system[0].state,db,reader,source,with_metadata=True)
        assert value['record_id']==child and value['version']==1 and value['source_id']==source and value['source_version']==1
        assert 'needle' in value['text'] and isinstance(context(system[0].state,db,reader,source),str)
        db.get(Record,child).sensitivity='SECRET';db.commit()
        with pytest.raises(HTTPException) as error:context(system[0].state,db,reader,source)
        assert error.value.status_code==403


@pytest.mark.asyncio
async def test_family_parent_never_expands_access_to_private_child(system,alice):
    bob=SignedClient(system[1],system[2]);source,child=await parsed_file(system,bob)
    assert bob.request('PUT','/api/v1/data/'+source+'/visibility',{'visibility':'family'}).status_code==200
    with system[2]() as db:
        scope(db,bob.user_id,'h1');db.get(Record,child).visibility='personal';db.commit()
        reader=Actor(alice.user_id,'h1',alice.device_id,'adult')
        with pytest.raises(HTTPException) as error:read_authorized_record(db,reader,child,binding(source,child))
        assert error.value.status_code==404
    assert alice.request('GET','/api/v1/data/'+source).status_code==200


@pytest.mark.asyncio
async def test_private_parent_grant_allows_bounded_literal_derived_search_then_invalidates(system,alice):
    bob=SignedClient(system[1],system[2]);source,child=await parsed_file(system,bob)
    task_id=new_task(system,alice,[bob]);action=request_action(system,alice,task_id,'data.share',{'record_ids':[source]},bob.user_id)
    assert response(bob,action,record_ids=[source]).status_code==200
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,task_id)
        body=system[0].state.vault.open(task.request,alice.user_id+':task:'+task_id)
        reader=Actor(alice.user_id,'h1',alice.device_id,'adult')
        result=task_documents_search(system[0].state,db,reader,body,'needle')
        assert result['mode']=='authorized_literal' and result['matches'][0]['record_id']==child
        assert 'needle' in result['matches'][0]['excerpt'] and len(result['matches'][0]['excerpt'])<=1000
        assert body['_derived_task_sources'][child]=={'source_id':source,'version':1,'source_version':1}
        result['matches'][0]['excerpt']='不能信任旧工具回执里的伪造内容'
        restored=task_rehydrate(system[0].state,db,reader,body,result)
        assert 'needle' in restored['matches'][0]['excerpt'] and '伪造内容' not in restored['matches'][0]['excerpt']
        assert read_authorized_record(db,reader,child,body).id==child
        task.request=system[0].state.vault.seal(body,alice.user_id+':task:'+task_id);db.commit()
    assert alice.request('GET','/api/v1/tasks/'+task_id+'/data/'+child).status_code==200
    with system[2]() as db:
        scope(db,bob.user_id,'h1');db.get(Record,child).version=2;db.commit()
    assert alice.request('GET','/api/v1/tasks/'+task_id+'/data/'+child).status_code==403
    with system[2]() as db:
        scope(db,bob.user_id,'h1');db.get(Record,child).version=1;db.get(Record,child).sensitivity='SECRET';db.commit()
    assert alice.request('GET','/api/v1/tasks/'+task_id+'/data/'+child).status_code==403
    bob.request('POST','/api/v1/client-actions/'+action+'/revoke')
    assert alice.request('GET','/api/v1/tasks/'+task_id+'/data/'+source).status_code==403


@pytest.mark.asyncio
async def test_derived_parent_version_and_relation_are_bound(system,alice):
    source,child=await parsed_file(system,alice)
    reader=Actor(alice.user_id,'h1',alice.device_id,'adult')
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        body=binding(source,child)
        assert read_authorized_record(db,reader,child,body).id==child
        db.get(Record,source).version=2;db.commit()
        with pytest.raises(HTTPException):read_authorized_record(db,reader,child,body)
        db.get(Record,source).version=1;db.get(Record,child).kind='note';db.commit()
        with pytest.raises(HTTPException):read_authorized_record(db,reader,child,body)


@pytest.mark.asyncio
async def test_explicit_derived_only_grant_never_expands_to_original_file(system,alice):
    bob=SignedClient(system[1],system[2]);source,child=await parsed_file(system,bob)
    tid=new_task(system,alice,[bob]);aid=request_action(system,alice,tid,'data.share',{'record_ids':[child]},bob.user_id)
    assert response(bob,aid,record_ids=[child]).status_code==200
    with system[2]() as db:
        scope(db,alice.user_id,'h1');task=db.get(Task,tid)
        body=system[0].state.vault.open(task.request,alice.user_id+':task:'+tid)
        reader=Actor(alice.user_id,'h1',alice.device_id,'adult')
        result=task_documents_search(system[0].state,db,reader,body,'needle')
        assert result['matches'][0]['record_id']==child
        assert 'needle' in task_rehydrate(system[0].state,db,reader,body,result)['matches'][0]['excerpt']
        with pytest.raises(HTTPException):read_authorized_record(db,reader,source,body)
    assert alice.request('GET','/api/v1/tasks/'+tid+'/assets/'+source).status_code==404
