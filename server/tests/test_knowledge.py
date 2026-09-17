import pytest
from fastapi import HTTPException
from homeai.knowledge import chunks,rehydrate
from homeai.result_access import check_dependencies
from homeai.security import Actor
from homeai.db import scope
from test_security_data import put


def test_chunk_ranges_cover_full_unicode_document():
    content='中文文档与 Straße 数据。'*400
    ranges=list(chunks(content))
    assert ranges[0][0]==0 and ranges[-1][1]==len(content)
    assert all(0<end-start<=1000 for start,end in ranges)
    assert all(right[0]<left[1] for left,right in zip(ranges,ranges[1:]))
    assert list(chunks(''))==[]
    unicode_text='🧠'*1800
    assert all(len(unicode_text[start:end].encode('utf8'))<=1800 for start,end in chunks(unicode_text))
    with pytest.raises(ValueError):list(chunks('text',size=10,overlap=10))


def test_changed_document_invalidates_cached_result(system,alice):
    item={'source':'notes','source_id':'document-version','kind':'document.parsed','version':1,'payload':{'markdown':'original document'}}
    rid=put(alice,item)
    result={'mode':'authorized_literal','matches':[{'record_id':rid,'version':1,'start':0,'end':8}]}
    actor=Actor(alice.user_id,'h1',alice.device_id,'adult')
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        assert rehydrate(db,actor,result,system[0].state.vault)['matches'][0]['excerpt']=='original'
    item['version']=2;item['payload']['markdown']='updated document'
    put(alice,item)
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        with pytest.raises(HTTPException):rehydrate(db,actor,result,system[0].state.vault)
        with pytest.raises(HTTPException):check_dependencies(db,actor,{'_record_dependencies':{rid:1}})


def test_result_guard_refreshes_previously_loaded_record(system,alice):
    from homeai.data import read_record
    item={'source':'notes','source_id':'guard-refresh','kind':'note','version':1,'payload':{'text':'old'}}
    rid=put(alice,item)
    actor=Actor(alice.user_id,'h1',alice.device_id,'adult')
    with system[2]() as db:
        scope(db,alice.user_id,'h1')
        cached=read_record(db,actor,rid)
        db.commit()
        item['version']=2;item['payload']={'text':'new'}
        put(alice,item)
        with pytest.raises(HTTPException):check_dependencies(db,actor,{'_record_dependencies':{rid:1}})
        assert cached.version==2
