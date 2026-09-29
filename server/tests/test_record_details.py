"""完整详情与二进制读取的真实API契约；仅使用隔离测试记录。"""
import base64
from sqlalchemy import select
from conftest import SignedClient
from homeai.db import Record,scope
from homeai.documents import persist
from homeai.security import Actor
from test_security_data import put,record


def parsed(system,alice,source):
    with system[2]() as db:
        value=persist(db,Actor(alice.user_id,'h1',alice.device_id,'adult'),source,1,{'markdown':'完整正文\n'+'段落内容'*3000},system[0].state)
        db.commit();return value['record_id']


def test_file_details_full_text_source_version_and_visibility(system,alice):
    bob=SignedClient(system[1],system[2]);outsider=SignedClient(system[1],system[2],household='other')
    source=put(alice,record(kind='document.file',payload={'name':'完整资料.pdf'}))
    child=parsed(system,alice,source)
    path='/api/v1/files/'+source+'/details'
    detail=alice.request('GET',path).json()
    assert detail['parse_status']=='ready'
    assert len(detail['parsed']['payload']['markdown'])>8000
    assert bob.request('GET',path).status_code==404
    assert alice.request('PUT','/api/v1/data/'+child+'/visibility',{'visibility':'family'}).status_code==403
    alice.request('PUT','/api/v1/data/'+source+'/visibility',{'visibility':'family'})
    assert bob.request('GET',path).json()['parsed']['id']==child
    assert outsider.request('GET',path).status_code==404
    put(alice,record(kind='document.file',version=2,payload={'name':'更新后的原件.pdf'}))
    assert alice.request('GET',path).json()['parse_status']=='stale'
    assert alice.request('GET',path).json()['parsed'] is None
    assert alice.request('GET','/api/v1/data/'+child).status_code==409
    alice.request('PUT','/api/v1/data/'+source+'/visibility',{'visibility':'personal'})
    assert bob.request('GET',path).status_code==404
    assert bob.request('GET','/api/v1/data/'+child).status_code==404


def test_photo_content_is_real_bytes_and_safe_filename(system,alice):
    bob=SignedClient(system[1],system[2])
    image=b'\x89PNG\r\n\x1a\n'+b'isolated-test-bytes'
    rid=put(alice,record(source_id='photo-detail',kind='photo.selected',payload={'name':'照片\r\nX-Injected: yes.png','content_base64':base64.b64encode(image).decode()}))
    path='/api/v1/files/'+rid+'/content'
    response=alice.request('GET',path)
    assert response.status_code==200 and response.content==image
    assert response.headers['content-type']=='image/png'
    assert response.headers['cache-control']=='no-store'
    assert 'x-injected' not in response.headers
    assert '\r' not in response.headers['content-disposition']
    assert bob.request('GET',path).status_code==404
    alice.request('PUT','/api/v1/data/'+rid+'/visibility',{'visibility':'family'})
    assert bob.request('GET',path).content==image
    alice.request('PUT','/api/v1/data/'+rid+'/visibility',{'visibility':'personal'})
    assert bob.request('GET',path).status_code==404


def test_unrelated_owner_cannot_forge_parsed_association(system,alice):
    bob=SignedClient(system[1],system[2])
    source=put(alice,record(kind='document.file',payload={'name':'原件'}))
    put(bob,{'source':'document_parse','source_id':source,'kind':'document.parsed','version':1,'payload':{'markdown':'伪造正文','source_version':1}})
    response=alice.request('GET','/api/v1/files/'+source+'/details')
    assert response.status_code==200 and response.json()['parsed'] is None


def test_legacy_shared_child_is_hidden_when_source_private(system,alice):
    bob=SignedClient(system[1],system[2])
    source=put(alice,record(kind='document.file',payload={'name':'私有原件'}));child=parsed(system,alice,source)
    with system[2]() as db:
        scope(db,alice.user_id,'h1');db.get(Record,child).visibility='family';db.commit()
    assert bob.request('GET','/api/v1/data/'+child).status_code==404
    assert not any(r['id']==child for r in bob.request('GET','/api/v1/data').json()['records'])
    alice.request('PUT','/api/v1/data/'+source+'/visibility',{'visibility':'personal'})
    assert alice.request('GET','/api/v1/data/'+child).json()['visibility']=='personal'


def test_postgres_record_detail_permissions():
    import os
    import pytest
    if os.getenv('HOMEAI_INTEGRATION')!='1':pytest.skip('需要真实 PostgreSQL')
    from fastapi.testclient import TestClient
    from homeai.config import Settings
    from homeai.api import create_app
    settings=Settings();settings.database_url=settings.database_url.rsplit('/',1)[0]+'/homeai_test'
    app=create_app(settings)
    with TestClient(app) as client:
        alice=SignedClient(client,app.state.db)
        try:
            test_file_details_full_text_source_version_and_visibility((app,client,app.state.db),alice)
            test_photo_content_is_real_bytes_and_safe_filename((app,client,app.state.db),alice)
        finally:alice.request('DELETE','/api/v1/devices/'+alice.device_id)
    app.state.db.kw['bind'].dispose()
