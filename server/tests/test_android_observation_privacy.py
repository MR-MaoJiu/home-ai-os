"""普通安卓观察自动脱敏，受限资料仍确认；只替换设备与模型传输边界。"""
import json
from unittest.mock import AsyncMock
from xml.sax.saxutils import quoteattr

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from homeai import android_adb, android_tools
from homeai.cloud_gateway import CloudConsentRequired, invoke
from homeai.contracts import ProviderManifest, TaskRequest
from homeai.db import Disclosure, MemoryCandidate, Provider, Record
from homeai.model_routing import ModelVerification, RedactionArtifact, fingerprint
from homeai.providers import Registry
from homeai.record_channels import is_business_data, is_memory
from homeai.runtime import submit
from test_android_workspace import workspace
from test_cloud_gateway import setup_cloud


@pytest.fixture
async def observation_gateway(system, alice, workspace, monkeypatch):
    app, actor, executed = workspace
    cloud = setup_cloud(system, alice)
    privacy = ProviderManifest(id='android.privacy.test', version='1', adapter='openai',
        endpoint='http://local.test/v1', model='privacy',
        capabilities={'model.generate@v1': 'chat'}, allowed_hosts=['local.test'])
    with app.db() as db:
        db.add(Provider(id=privacy.id, manifest=privacy.model_dump_json(), enabled=True))
        db.add(ModelVerification(id='h1:' + privacy.id, household_id='h1', provider_id=privacy.id,
            fingerprint=fingerprint(privacy), report=json.dumps({'privacy': True, 'text': True})))
        db.commit()
    response = alice.request('PUT', '/api/v1/manage/models', {
        'local_privacy': privacy.id, 'cloud_planner': cloud.id, 'shared_cloud': True,
        'household_daily_tokens': 100000, 'member_daily_tokens': 50000})
    assert response.status_code == 200, response.text
    traffic = []

    def transport(request):
        body = json.loads(request.content)
        traffic.append((request.url.host, body))
        if request.url.host == 'local.test':
            content = body['messages'][-1]['content']
            if '"safe"' in body['messages'][0]['content']:
                detected = {'safe': True, 'uncertain': False, 'reason_codes': []}
            else:
                category = ('health' if '健康指标样例' in content else
                    'precise_location' if '精确坐标样例' in content else None)
                detected = {'entities': ['测试甲'] if '测试甲' in content else [],
                    'sensitive': category is not None, 'uncertain': False,
                    'reason_codes': [category] if category else []}
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(detected)}}]})
        assert request.url.host == 'model.example'
        return httpx.Response(200, json={'choices': [{'message': {'content': '公开测试回复'}}],
            'usage': {'total_tokens': 12}})

    app.registry = Registry(app.vault, httpx.MockTransport(transport))
    monkeypatch.setattr(android_adb, 'focus_signature',
        AsyncMock(side_effect=HTTPException(409, '公开测试页面没有输入焦点')))
    return app, actor, cloud, traffic, executed


def new_task(app, actor, db):
    task = submit(db, actor, TaskRequest(message='操作公开测试界面',
        idempotency_key='android-observation-privacy-test'), app.vault)
    body = app.vault.open(task.request, actor.user_id + ':task:' + task.id)
    db.commit()
    return task, body


async def observe(app, actor, db, task, body, monkeypatch, text, number):
    xml = ('<hierarchy><node text=' + quoteattr(text) +
        ' package="com.example.test" clickable="true" password="false"'
        ' bounds="[0,0][160,80]"/></hierarchy>').encode()
    monkeypatch.setattr(android_adb, 'hierarchy', AsyncMock(return_value=xml))
    result = await android_tools.execute(app, db, actor, task, body, 'android.observe@v1',
        {'purpose': '读取公开测试界面'}, 'android-privacy-observe-' + str(number))
    return result


def arguments(result, *, images=()):
    parts = [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}]
    parts.extend(images)
    return {'messages': [{'role': 'user', 'content': parts}], 'max_tokens': 128}


@pytest.mark.asyncio
async def test_repeated_normal_observations_use_gateway_without_frame_consents(
        observation_gateway, alice, monkeypatch):
    app, actor, cloud, traffic, executed = observation_gateway
    with app.db() as db:
        task, body = new_task(app, actor, db)
        identifiers = []
        for number, text in enumerate(['首页', '商品详情', '返回列表'], 1):
            result = await observe(app, actor, db, task, body, monkeypatch, text, number)
            identifiers.append(result['record_id'])
            reply = await invoke(app, db, actor, cloud, task, body, arguments(result), 'normal-' + str(number))
            assert reply['choices'][0]['message']['content'] == '公开测试回复'
        assert len(set(identifiers)) == 3 and set(body['_record_dependencies']) == set(identifiers)
        records = list(db.scalars(select(Record).where(Record.id.in_(identifiers))))
        assert all(row.cloud_policy == 'REDACT_AND_ALLOW' for row in records)
        assert all(row.visibility == 'personal' and not is_business_data(row) and not is_memory(row) for row in records)
        assert db.scalar(select(MemoryCandidate)) is None
        from homeai.client_actions import ClientAction
        assert db.scalar(select(ClientAction).where(ClientAction.kind == 'cloud.disclose')) is None
        assert len(list(db.scalars(select(Disclosure).where(Disclosure.task_id == task.id)))) == 3
    assert len([host for host, _ in traffic if host == 'local.test']) == 6
    assert len([host for host, _ in traffic if host == 'model.example']) == 3
    executed.assert_not_awaited()
    listed = alice.request('GET', '/api/v1/data')
    assert listed.status_code == 200 and all(identifier not in listed.text for identifier in identifiers)


@pytest.mark.asyncio
async def test_normal_ui_pii_is_redacted_with_encrypted_provenance(observation_gateway, monkeypatch):
    app, actor, cloud, traffic, _ = observation_gateway
    with app.db() as db:
        task, body = new_task(app, actor, db)
        result = await observe(app, actor, db, task, body, monkeypatch,
            '联系人测试甲：13800138000，邮件 person@example.com', 1)
        await invoke(app, db, actor, cloud, task, body, arguments(result), 'normal-pii')
        outgoing = json.dumps([data for host, data in traffic if host == 'model.example'], ensure_ascii=False)
        assert all(value not in outgoing for value in ['测试甲', '13800138000', 'person@example.com'])
        artifact = db.scalar(select(RedactionArtifact).where(RedactionArtifact.task_id == task.id))
        provenance = app.vault.open(artifact.payload, actor.user_id + ':redaction:' + artifact.id)
        assert provenance['versions'] == body['_record_dependencies']
        assert set(provenance['mapping'].values()) == {'测试甲', '13800138000', 'person@example.com'}
        assert 'person@example.com' not in artifact.payload


@pytest.mark.asyncio
@pytest.mark.parametrize(('text', 'reason'), [('健康指标样例', '健康信息'), ('精确坐标样例', '精确位置')])
async def test_new_sensitive_frame_still_requires_current_task_consent(observation_gateway, monkeypatch, text, reason):
    app, actor, cloud, traffic, _ = observation_gateway
    with app.db() as db:
        task, body = new_task(app, actor, db)
        first = await observe(app, actor, db, task, body, monkeypatch, '公开首页', 1)
        await invoke(app, db, actor, cloud, task, body, arguments(first), 'before-sensitive')
        result = await observe(app, actor, db, task, body, monkeypatch, text, 2)
        with pytest.raises(CloudConsentRequired) as consent:
            await invoke(app, db, actor, cloud, task, body, arguments(result), 'sensitive-frame')
        assert reason in consent.value.detail
        assert consent.value.record_versions == body['_record_dependencies']
        assert result['record_id'] in consent.value.record_versions
    assert len([host for host, _ in traffic if host == 'model.example']) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('restricted_source', ['legacy_local_only', 'original_image'])
async def test_ordinary_ui_does_not_relax_existing_local_policy_or_original_media(
        observation_gateway, alice, monkeypatch, restricted_source):
    app, actor, cloud, traffic, _ = observation_gateway
    with app.db() as db:
        task, body = new_task(app, actor, db)
        result = await observe(app, actor, db, task, body, monkeypatch, '公开页面', 1)
        images = []
        if restricted_source == 'legacy_local_only':
            db.get(Record, result['record_id']).cloud_policy = 'LOCAL_ONLY'
            db.commit()
        else:
            from homeai.media import cloud_images
            from test_media import upload, png
            db.commit()
            uploaded = upload(alice, png(), '公开像素样例.png', 'image')
            assert uploaded.status_code == 200, uploaded.text
            record_id = uploaded.json()['asset']['record_id']
            body['_record_dependencies'][record_id] = db.get(Record, record_id).version
            images = await cloud_images(app, db, actor, record_id)
        with pytest.raises(CloudConsentRequired) as consent:
            await invoke(app, db, actor, cloud, task, body,
                arguments(result, images=images), 'restricted-ui')
        assert consent.value.record_versions == body['_record_dependencies']
        assert ('仅本地处理' if restricted_source == 'legacy_local_only' else '图像') in consent.value.detail
    assert not any(host == 'model.example' for host, _ in traffic)


@pytest.mark.asyncio
async def test_normal_ui_still_requires_other_members_cloud_consent(
        observation_gateway, system, monkeypatch):
    from conftest import SignedClient
    from homeai.contracts import DataRecord
    from homeai.data import ingest
    from homeai.security import Actor
    app, actor, cloud, traffic, _ = observation_gateway
    bob = SignedClient(system[1], system[2])
    with app.db() as db:
        source = ingest(db, Actor(bob.user_id, 'h1', bob.device_id, 'adult'),
            DataRecord(source='test_shared_source', source_id='public-test-shared',
                kind='document.import', version=1, cloud_policy='REDACT_AND_ALLOW',
                payload={'text': '其他成员共享的公开样例'}), app.vault)
        source.visibility = 'family'
        record_id = source.id
        db.commit()
    with app.db() as db:
        task, body = new_task(app, actor, db)
        result = await observe(app, actor, db, task, body, monkeypatch, '公开页面', 1)
        body['_record_dependencies'][record_id] = 1
        with pytest.raises(CloudConsentRequired) as consent:
            await invoke(app, db, actor, cloud, task, body, arguments(result), 'another-member-source')
        assert consent.value.target_member_id == bob.user_id
        assert consent.value.record_versions == {record_id: 1}
    assert not any(host == 'model.example' for host, _ in traffic)


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked_source', ['secret_record', 'deleted_record', 'changed_version'])
async def test_ui_source_reclassification_deletion_or_revision_blocks_cloud(
        observation_gateway, monkeypatch, blocked_source):
    app, actor, cloud, traffic, _ = observation_gateway
    with app.db() as db:
        task, body = new_task(app, actor, db)
        result = await observe(app, actor, db, task, body, monkeypatch, '公开页面', 1)
        row = db.get(Record, result['record_id'])
        if blocked_source == 'secret_record':
            row.sensitivity = 'SECRET'
        elif blocked_source == 'deleted_record':
            row.deleted = True
        else:
            row.version += 1
        db.commit()
        with pytest.raises(HTTPException) as failure:
            await invoke(app, db, actor, cloud, task, body, arguments(result), 'invalidated-ui')
        assert failure.value.status_code == {'secret_record': 403, 'deleted_record': 404, 'changed_version': 409}[blocked_source]
    assert traffic == []


@pytest.mark.asyncio
async def test_secret_ui_text_is_blocked_before_model_transport(observation_gateway, monkeypatch):
    app, actor, cloud, traffic, _ = observation_gateway
    with app.db() as db:
        task, body = new_task(app, actor, db)
        result = await observe(app, actor, db, task, body, monkeypatch, 'api_key=public-secret-sample', 1)
        with pytest.raises(HTTPException) as failure:
            await invoke(app, db, actor, cloud, task, body, arguments(result), 'secret-ui-text')
        assert failure.value.status_code == 422
    assert traffic == []
