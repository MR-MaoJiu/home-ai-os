"""真实媒体字节、规范附件与任务结果投影；不调用付费图片服务。"""
import uuid
import pytest
from homeai.runtime import run_task
from homeai.db import Task, Record, now
from homeai.security import Actor
from homeai.resource_assets import store_asset
from homeai.chat_outputs import ChatOutputPart
from conftest import SignedClient
from test_media import upload, png, video


def actor_for(alice):
    return Actor(alice.user_id, 'h1', alice.device_id, 'infrastructure_owner')


@pytest.mark.parametrize('kind,name,raw', [('image','原图.png',png()), ('video','原视频.mp4',video()), ('file','资料.txt','公开资料正文'.encode())])
async def test_existing_media_is_delivered_as_real_attachment(system, alice, kind, name, raw):
    uploaded = upload(alice, raw, name, kind).json()['asset']
    response = alice.request('POST','/api/v1/tasks',{'idempotency_key':str(uuid.uuid4()),
        'capability':'resource.send@v1','arguments':{'record_id':uploaded['record_id']}})
    assert response.status_code == 202, response.text
    tid = response.json()['id']
    await run_task(system[0].state, tid, alice.user_id)
    result = alice.request('GET','/api/v1/tasks/'+tid).json()
    assert result['status'] == 'SUCCEEDED', result
    part = result['result']['parts'][0]
    assert part['type'] == kind and part['record_id'] == uploaded['record_id']
    ChatOutputPart.model_validate(part)
    assert alice.request('GET','/api/v1/assets/'+part['record_id']+'/content').content == raw
    bob = SignedClient(system[1], system[2])
    assert bob.request('GET','/api/v1/assets/'+part['record_id']+'/content').status_code == 404
    assert alice.request('DELETE','/api/v1/data/'+part['record_id']).status_code == 200
    assert alice.request('GET','/api/v1/tasks/'+tid).json()['result'] is None


def test_retrieved_assets_are_encrypted_idempotent_and_have_provenance(system, alice):
    app, _, factory = system
    raw = png()
    origin = {'origin':'retrieved','source_url':'https://example.org/original.png','retrieved_at':now()}
    with factory() as db:
        task = Task(id=str(uuid.uuid4()),owner_id=alice.user_id,household_id='h1',idempotency_key=str(uuid.uuid4()),request_hash='test',request='test',deadline=now()+600)
        db.add(task); db.flush()
        result = store_asset(app.state, db, actor_for(alice), task, 'public-resource-step', raw, '原图.png', 'image', origin)
        db.commit()
        again = store_asset(app.state, db, actor_for(alice), task, 'public-resource-step', raw, '原图.png', 'image', origin)
        assert result == again
        part = result['parts'][0]; ChatOutputPart.model_validate(part)
        record = db.get(Record, part['record_id'])
        folder = app.state.settings.state_dir/'media'/record.source_id
        assert raw not in (folder/'0.enc').read_bytes()
        assert b'example.org' not in (folder/'manifest.enc').read_bytes()
    asset = alice.request('GET','/api/v1/assets/'+part['record_id']).json()
    assert asset['provenance']['origin'] == 'retrieved'
    assert alice.request('GET','/api/v1/assets/'+part['record_id']+'/content').content == raw


def test_generation_tool_cannot_be_substituted_for_original_resource_request(system, alice):
    import asyncio
    from fastapi import HTTPException
    from homeai.image_generation import execute
    with system[2]() as db:
        for message in ('帮我找一张真实照片', '不要生成，搜索原图'):
            with pytest.raises(HTTPException, match='检索获取'):
                asyncio.run(execute(system[0].state, db, actor_for(alice), None,
                    {'_agent':True,'message':message}, 'image.generate@v1', {'prompt':'一个山谷'}, 'test'))


def test_explicit_resource_url_ignores_sentence_punctuation(system, alice):
    from homeai.resource_tools import allowed_urls
    from types import SimpleNamespace
    with system[2]() as db:
        urls = allowed_urls(system[0].state, db, actor_for(alice), SimpleNamespace(id='not-a-real-task'),
                            {'message':'请获取“https://example.org/manual.pdf”。'})
        assert urls == {'https://example.org/manual.pdf'}
