"""实际加密分块、文件解码与权限检查；不伪造解析器或模型响应。"""
import base64
import hashlib
import os
import struct
import uuid
import zlib
import pytest
from sqlalchemy import select
from homeai.media import CHUNK_SIZE, MediaUpload, MediaJob, MediaPending, cleanup, context, process_user, router, validate_parts
from homeai.db import Record, now, scope
from homeai.security import Actor
from conftest import SignedClient


@pytest.fixture
def media_system(system):
    app,client,db=system
    if not any(getattr(route,'path','')=='/api/v1/uploads' for route in app.routes):app.include_router(router)
    return system


def create(user,raw,name='验收.txt',kind='file',client_id=None):
    body={'client_id':client_id or str(uuid.uuid4()),'name':name,'kind':kind,'mime_type':'application/octet-stream','size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    response=user.request('POST','/api/v1/uploads',body)
    assert response.status_code==200,response.text
    return response.json(),body


def chunk(user,identifier,index,raw):
    path=f'/api/v1/uploads/{identifier}/chunks/{index}'
    headers=user.headers('PUT',path,raw);headers['content-type']='application/octet-stream'
    return user.client.put(path,content=raw,headers=headers)


def upload(user,raw,name='验收.txt',kind='file'):
    result,_=create(user,raw,name,kind)
    for index,offset in enumerate(range(0,len(raw),CHUNK_SIZE)):
        response=chunk(user,result['upload_id'],index,raw[offset:offset+CHUNK_SIZE])
        assert response.status_code==200,response.text
    return user.request('POST',f"/api/v1/uploads/{result['upload_id']}/complete")


def png():
    def block(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+block(b'IHDR',struct.pack('>IIBBBBB',2,2,8,2,0,0,0))+block(b'IDAT',zlib.compress((b'\0'+b'\xff\0\0'*2)*2))+block(b'IEND',b'')


def test_real_chunk_resume_integrity_idempotence_and_encryption(media_system,alice):
    app,_,_=media_system
    raw=b'real-chunk-content-'+b'x'*CHUNK_SIZE
    first,body=create(alice,raw)
    identifier=first['upload_id'];base='/api/v1/uploads/'+identifier
    assert alice.request('POST','/api/v1/uploads',body).json()['upload_id']==identifier
    assert alice.request('POST','/api/v1/uploads',{**body,'name':'changed.txt'}).status_code==409
    assert chunk(alice,identifier,1,raw[CHUNK_SIZE:]).status_code==200
    assert alice.request('GET',base).json()['received_parts']==[1]
    assert alice.request('POST',base+'/complete').status_code==409
    assert chunk(alice,identifier,0,raw[:CHUNK_SIZE]).status_code==200
    assert chunk(alice,identifier,0,raw[:CHUNK_SIZE]).status_code==200
    assert chunk(alice,identifier,0,b'z'*CHUNK_SIZE).status_code==409
    response=alice.request('POST',base+'/complete');assert response.status_code==200,response.text
    asset=response.json()['asset'];assert asset['processing']['status']=='queued'
    assert alice.request('POST',base+'/complete').json()['asset']['record_id']==asset['record_id']
    encrypted=(app.state.settings.state_dir/'media'/identifier/'0.enc').read_bytes()
    assert b'real-chunk-content-' not in encrypted
    downloaded=alice.request('GET','/api/v1/assets/'+asset['record_id']+'/content?offset=0&length=1048576')
    assert downloaded.status_code==206 and downloaded.content==raw[:CHUNK_SIZE]
    assert alice.request('GET','/api/v1/assets/'+asset['record_id']+'/content?offset=1048576').content==raw[CHUNK_SIZE:]


def test_media_owner_family_revoke_and_part_versions(media_system,alice):
    app,client,factory=media_system
    bob=SignedClient(client,factory);outsider=SignedClient(client,factory,household='another')
    completed=upload(alice,b'private document').json();identifier=completed['upload_id'];asset=completed['asset'];rid=asset['record_id']
    assert bob.request('GET','/api/v1/uploads/'+identifier).status_code==404
    assert bob.request('DELETE','/api/v1/uploads/'+identifier).status_code==404
    assert outsider.request('GET','/api/v1/assets/'+rid).status_code==404
    assert alice.request('PUT','/api/v1/data/'+rid+'/visibility',{'visibility':'family'}).status_code==200
    assert bob.request('GET','/api/v1/assets/'+rid+'/content').content==b'private document'
    assert outsider.request('GET','/api/v1/assets/'+rid+'/content').status_code==404
    with factory() as db:
        actor=Actor(alice.user_id,'h1',alice.device_id,'infrastructure_owner')
        normalized,refs=validate_parts(app.state,db,actor,[{'type':'file','record_id':rid,'version':1}])
        assert refs=={rid:1} and normalized[0]['type']=='file'
        from fastapi import HTTPException
        for invalid in [{'type':'image','record_id':rid,'version':1},{'type':'file','record_id':rid,'version':2},{'type':'file','record_id':rid,'version':1,'url':'https://example.com'}]:
            with pytest.raises(HTTPException):validate_parts(app.state,db,actor,[invalid])
    assert alice.request('PUT','/api/v1/data/'+rid+'/visibility',{'visibility':'personal'}).status_code==200
    assert bob.request('GET','/api/v1/assets/'+rid+'/content').status_code==404


def test_real_image_decoder_thumbnail_and_invalid_content(media_system,alice):
    response=upload(alice,png(),'test.png','image')
    assert response.status_code==200,response.text
    asset=response.json()['asset'];assert asset['mime_type']=='image/png' and asset['width']==2
    thumb=alice.request('GET','/api/v1/assets/'+asset['record_id']+'/thumbnail')
    assert thumb.status_code==200 and thumb.content.startswith(b'\xff\xd8')
    invalid=upload(alice,b'not-an-image','invalid.png','image')
    assert invalid.status_code==422


def test_expired_cancel_and_deleted_chunks_are_cleaned(media_system,alice):
    app,_,factory=media_system
    raw=b'temporary'
    first,_=create(alice,raw);identifier=first['upload_id'];chunk(alice,identifier,0,raw)
    assert alice.request('DELETE','/api/v1/uploads/'+identifier).status_code==200
    assert alice.request('DELETE','/api/v1/uploads/'+identifier).status_code==200
    assert not (app.state.settings.state_dir/'media'/identifier).exists()
    assert chunk(alice,identifier,0,raw).status_code==409
    second,_=create(alice,raw);second_id=second['upload_id'];chunk(alice,second_id,0,raw)
    with factory() as db:
        scope(db,alice.user_id,'h1');db.get(MediaUpload,second_id).expires_at=now()-1;db.commit()
    cleanup(app.state,alice.user_id,'h1')
    assert not (app.state.settings.state_dir/'media'/second_id).exists()
    assert alice.request('POST','/api/v1/uploads/'+second_id+'/complete').status_code==409
    third=upload(alice,raw).json();rid=third['asset']['record_id']
    assert alice.request('DELETE','/api/v1/data/'+rid).status_code==200
    assert alice.request('GET','/api/v1/assets/'+rid+'/content').status_code==404
    cleanup(app.state,alice.user_id,'h1')
    assert not (app.state.settings.state_dir/'media'/third['upload_id']).exists()


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv('HOMEAI_INTEGRATION')!='1',reason='需要真实 OPA，不替换策略服务')
async def test_server_text_parser_and_pending_context(media_system,alice):
    from homeai.config import Settings
    from homeai.policy import Policy
    app,_,factory=media_system;app.state.policy=Policy(Settings().opa_url)
    result=upload(alice,'家庭附件真实解析：周六检查备份。'.encode()).json();rid=result['asset']['record_id']
    actor=Actor(alice.user_id,'h1',alice.device_id,'infrastructure_owner')
    with factory() as db:
        with pytest.raises(MediaPending):context(app.state,db,actor,rid)
    await process_user(app.state,alice.user_id,'h1')
    response=alice.request('GET','/api/v1/assets/'+rid).json();assert response['processing']['status']=='succeeded',response
    with factory() as db:assert '周六检查备份' in context(app.state,db,actor,rid)
    assert alice.request('GET','/api/v1/memory/entries').json()==[]


def video(seconds=1):
    import av
    import io
    output=io.BytesIO()
    with av.open(output,mode='w',format='mp4') as container:
        stream=container.add_stream('mpeg4',rate=24);stream.width=16;stream.height=16;stream.pix_fmt='yuv420p'
        for pts in (0,int(seconds*24)):
            frame=av.VideoFrame(16,16,'yuv420p')
            for plane in frame.planes:plane.update(bytes([128])*plane.buffer_size)
            frame.pts=pts
            for packet in stream.encode(frame):container.mux(packet)
        for packet in stream.encode():container.mux(packet)
    return output.getvalue()


def test_real_video_duration_and_upload_limits(media_system,alice):
    valid=upload(alice,video(),'clip.mp4','video')
    assert valid.status_code==200,valid.text
    metadata=valid.json()['asset'];assert 0<metadata['duration_seconds']<2
    assert alice.request('GET','/api/v1/assets/'+metadata['record_id']+'/thumbnail').status_code==200
    long=upload(alice,video(601),'long.mp4','video')
    assert long.status_code==422 and '10 分钟' in long.json()['detail']
    body={'client_id':str(uuid.uuid4()),'name':'large.png','kind':'image','mime_type':'image/png','size':20*CHUNK_SIZE+1,'sha256':'a'*64}
    assert alice.request('POST','/api/v1/uploads',body).status_code==413
    assert alice.request('POST','/api/v1/uploads',{**body,'kind':'video','size':200*CHUNK_SIZE+1}).status_code==413
    assert alice.request('POST','/api/v1/uploads',{**body,'kind':'file','size':50*CHUNK_SIZE+1}).status_code==413


def test_canonical_metadata_cannot_forge_other_attachment(media_system,alice):
    app,_,factory=media_system
    first=upload(alice,b'first').json()['asset']['record_id']
    second=upload(alice,b'second').json()['asset']['record_id']
    from homeai.data import serialize
    with factory() as db:
        scope(db,alice.user_id,'h1')
        record=db.get(Record,first);other=db.get(Record,second)
        forged=serialize(other,app.state.vault)['payload']
        forged['media_upload_id']=record.source_id
        record.payload=app.state.vault.seal(forged,alice.user_id+':record:'+record.id)
        db.commit()
    assert alice.request('GET','/api/v1/assets/'+first+'/content').status_code==409


def test_configured_limits_apply_before_upload_and_completion(media_system,alice):
    app,_,_=media_system
    initial=alice.request('GET','/api/v1/uploads/limits').json()
    assert initial['image_max_bytes']==20*CHUNK_SIZE and initial['video_max_seconds']==600
    created,_=create(alice,b'1234');chunk(alice,created['upload_id'],0,b'1234')
    app.state.settings.media_file_max_bytes=3
    assert alice.request('GET','/api/v1/uploads/limits').json()['file_max_bytes']==3
    assert alice.request('POST','/api/v1/uploads/'+created['upload_id']+'/complete').status_code==413
    body={'client_id':str(uuid.uuid4()),'name':'small.txt','kind':'file','mime_type':'text/plain','size':4,'sha256':hashlib.sha256(b'1234').hexdigest()}
    assert alice.request('POST','/api/v1/uploads',body).status_code==413


@pytest.mark.asyncio
async def test_cloud_frames_are_real_bounded_jpeg_and_never_send(media_system,alice):
    import av
    import io
    from homeai.media import cloud_images
    app,_,factory=media_system
    actor=Actor(alice.user_id,'h1',alice.device_id,'infrastructure_owner')
    for raw,name,kind in ((png(),'image.png','image'),(video(),'clip.mp4','video')):
        result=upload(alice,raw,name,kind);assert result.status_code==200,result.text
        rid=result.json()['asset']['record_id']
        with factory() as db:
            parts=await cloud_images(app.state,db,actor,rid)
            assert await cloud_images(app.state,db,actor,rid)==parts
        assert 1<=len(parts)<=4
        for part in parts:
            value=part['image_url']['url'];assert value.startswith('data:image/jpeg;base64,')
            binary=base64.b64decode(value.split(',',1)[1],validate=True)
            assert len(binary)<=512*1024
            with av.open(io.BytesIO(binary)) as decoded:
                frame=next(decoded.decode(video=0));assert max(frame.width,frame.height)<=1024
    file=upload(alice,b'plain text').json()['asset']['record_id']
    from fastapi import HTTPException
    with factory() as db:
        with pytest.raises(HTTPException):await cloud_images(app.state,db,actor,file)


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv('HOMEAI_INTEGRATION')!='1',reason='需要真实 OPA，不替换策略服务')
async def test_missing_local_vision_has_bounded_retries_and_explicit_failure(media_system,alice):
    from homeai.config import Settings
    from homeai.policy import Policy
    from fastapi import HTTPException
    app,_,factory=media_system;app.state.policy=Policy(Settings().opa_url)
    result=upload(alice,png(),'no-model.png','image').json();rid=result['asset']['record_id']
    for attempt in range(1,4):
        with factory() as db:
            scope(db,alice.user_id,'h1');job=db.scalar(select(MediaJob).where(MediaJob.record_id==rid));job.retry_at=0;db.commit()
        await process_user(app.state,alice.user_id,'h1')
        current=alice.request('GET','/api/v1/assets/'+rid).json()['processing']
        assert current['status']==('retrying' if attempt<3 else 'failed'),current
    with factory() as db:
        with pytest.raises(HTTPException) as failure:context(app.state,db,Actor(alice.user_id,'h1',alice.device_id,'infrastructure_owner'),rid)
        assert not isinstance(failure.value,MediaPending)
    assert alice.request('POST','/api/v1/assets/'+rid+'/retry').json()['status']=='queued'
