import uuid
import os
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from homeai.artifact_sources import ArtifactSource
from homeai.contracts import DataRecord
from homeai.data import emit, ingest
from homeai.db import Record, Task, now, scope
from homeai.media import validate_parts
from homeai.resource_assets import store_asset
from homeai.security import Actor
from conftest import SignedClient
from test_media import upload


def actor(client, household='h1'):
    return Actor(client.user_id, household, client.device_id, 'infrastructure_owner')


def source(alice):
    result = upload(alice, b'2\n3\n5\n', 'numbers.txt', 'file')
    assert result.status_code == 200, result.text
    return result.json()['asset']['record_id']


def artifact(system, alice, versions, *, name='result.txt', household='h1'):
    with system[2]() as db:
        scope(db, alice.user_id, household)
        result = store_asset(system[0].state, db, actor(alice, household), None, str(uuid.uuid4()), b'10\n', name, 'file',
            {'origin': 'shell_created', 'created_at': now(), 'source_versions': versions})
        db.commit()
        return result['parts'][0]['record_id']


def assert_unavailable(alice, identifier):
    assert alice.request('GET', '/api/v1/assets/' + identifier + '/content').status_code == 404
    assert alice.request('GET', '/api/v1/assets/' + identifier).status_code == 404
    assert alice.request('GET', '/api/v1/data/' + identifier).status_code == 404
    assert identifier not in {item['id'] for item in alice.request('GET', '/api/v1/data').json()['records']}


def test_deleting_source_hides_file_metadata_and_model_attachment_input(system, alice):
    original = source(alice)
    derived = artifact(system, alice, {original: 1})
    assert alice.request('GET', '/api/v1/assets/' + derived + '/content').content == b'10\n'
    assert alice.request('DELETE', '/api/v1/data/' + original).status_code == 200
    assert_unavailable(alice, derived)
    with system[2]() as db:
        with pytest.raises(HTTPException):
            validate_parts(system[0].state, db, actor(alice), [{'type': 'file', 'record_id': derived, 'version': 1}])


@pytest.mark.parametrize('change', ['secret', 'version', 'missing'])
def test_invalid_source_hides_artifact_and_existing_task_projection(system, alice, change):
    original = source(alice)
    derived = artifact(system, alice, {original: 1})
    task_id = str(uuid.uuid4())
    app = system[0].state
    with system[2]() as db:
        db.add(Task(id=task_id, owner_id=alice.user_id, household_id='h1', idempotency_key=task_id,
            request_hash='test', request=app.vault.seal({'record_ids': [], '_output_resources': {derived: 1}}, alice.user_id + ':task:' + task_id),
            result=app.vault.seal({'text': '10'}, alice.user_id + ':task-result:' + task_id), status='SUCCEEDED', deadline=now() + 60))
        row = db.get(Record, original)
        if change == 'secret':
            row.sensitivity = 'SECRET'
        elif change == 'version':
            row.version += 1
        else:
            db.delete(row)
        db.commit()
    assert_unavailable(alice, derived)
    view = alice.request('GET', '/api/v1/tasks/' + task_id)
    assert view.status_code == 200, view.text
    assert view.json()['result'] is None


def test_all_ancestor_sources_are_bound_not_only_immediate_parent(system, alice):
    original = source(alice)
    first = artifact(system, alice, {original: 1})
    second = artifact(system, alice, {first: 1}, name='second.txt')
    with system[2]() as db:
        rows = db.scalars(select(ArtifactSource).where(ArtifactSource.record_id == second)).all()
        assert {row.source_id: row.source_version for row in rows} == {original: 1, first: 1}
    assert alice.request('DELETE', '/api/v1/data/' + original).status_code == 200
    assert_unavailable(alice, first)
    assert_unavailable(alice, second)


def test_parsed_text_cannot_bypass_its_shell_artifact_parent(system, alice):
    original = source(alice)
    derived = artifact(system, alice, {original: 1})
    with system[2]() as db:
        parsed = ingest(db, actor(alice), DataRecord(source='document_parse', source_id=derived,
            kind='document.parsed', version=1, sensitivity='PRIVATE', payload={'markdown': '10', 'source_ids': [derived], 'source_version': 1}), system[0].state.vault)
        identifier = parsed.id
        db.commit()
    assert alice.request('GET', '/api/v1/data/' + identifier).status_code == 200
    assert alice.request('DELETE', '/api/v1/data/' + original).status_code == 200
    assert alice.request('GET', '/api/v1/data/' + identifier).status_code == 404


def test_artifact_from_parsed_context_inherits_original_file_chain(system, alice):
    original = source(alice)
    first = artifact(system, alice, {original: 1})
    with system[2]() as db:
        parsed = ingest(db, actor(alice), DataRecord(source='document_parse', source_id=first,
            kind='document.parsed', version=1, sensitivity='PRIVATE', payload={'markdown': '10', 'source_ids': [first], 'source_version': 1}), system[0].state.vault)
        parsed_id = parsed.id
        db.commit()
    second = artifact(system, alice, {parsed_id: 1})
    with system[2]() as db:
        assert set(db.scalars(select(ArtifactSource.source_id).where(ArtifactSource.record_id == second))) == {original, first, parsed_id}
    assert alice.request('DELETE', '/api/v1/data/' + original).status_code == 200
    assert_unavailable(alice, second)


def test_shared_artifact_cannot_expand_or_outlive_source_visibility(system, alice):
    original = source(alice)
    derived = artifact(system, alice, {original: 1})
    bob = SignedClient(system[1], system[2])
    assert alice.request('PUT', '/api/v1/data/' + derived + '/visibility', {'visibility': 'family'}).status_code == 200
    assert bob.request('GET', '/api/v1/assets/' + derived + '/content').status_code == 404
    assert alice.request('PUT', '/api/v1/data/' + original + '/visibility', {'visibility': 'family'}).status_code == 200
    assert bob.request('GET', '/api/v1/assets/' + derived + '/content').content == b'10\n'
    assert alice.request('PUT', '/api/v1/data/' + original + '/visibility', {'visibility': 'personal'}).status_code == 200
    assert bob.request('GET', '/api/v1/assets/' + derived + '/content').status_code == 404
    assert alice.request('GET', '/api/v1/assets/' + derived + '/content').status_code == 200


def test_cross_member_source_cannot_be_copied_into_owned_shell_artifact(system, alice):
    original = source(alice)
    assert alice.request('PUT', '/api/v1/data/' + original + '/visibility', {'visibility': 'family'}).status_code == 200
    bob = SignedClient(system[1], system[2])
    with pytest.raises(HTTPException, match='本人成员'):
        artifact(system, bob, {original: 1})


def test_incremental_sync_removes_all_cached_descendants(system, alice):
    original = source(alice)
    first = artifact(system, alice, {original: 1})
    second = artifact(system, alice, {first: 1})
    with system[2]() as db:
        parsed = ingest(db, actor(alice), DataRecord(source='document_parse', source_id=first,
            kind='document.parsed', version=1, sensitivity='PRIVATE', payload={'markdown': '10', 'source_ids': [first], 'source_version': 1}), system[0].state.vault)
        parsed_id = parsed.id
        db.commit()
    snapshot = alice.request('POST', '/api/v1/sync/snapshot').json()
    page = alice.request('GET', '/api/v1/sync/snapshot/' + snapshot['snapshot_id']).json()
    assert page['done']
    assert alice.request('POST', '/api/v1/sync/ack', {'cursor': page['watermark'], 'snapshot_id': snapshot['snapshot_id']}).status_code == 200
    with system[2]() as db:
        row = db.get(Record, original)
        row.version += 1
        emit(db, actor(alice), 'record.changed', original)
        db.commit()
    changes = alice.request('GET', '/api/v1/sync/changes').json()
    assert {first, second, parsed_id} <= set(changes['removed_ids'])
    assert first not in {item['id'] for item in changes['records']}


def test_replayed_deletion_journal_prevents_restored_artifact_resurrection(system, alice, tmp_path):
    from homeai.backup import replay_deletions
    original = source(alice)
    derived = artifact(system, alice, {original: 1})
    journal = tmp_path / 'independent-deletions.jsonl'
    journal.write_text(system[0].state.vault.seal({'record_id': original, 'owner_id': alice.user_id}, 'deletion-journal') + '\n')
    replay_deletions(system[2], system[0].state.vault, journal)
    assert_unavailable(alice, derived)


def test_standalone_shell_report_without_data_sources_remains_accessible(system, alice):
    identifier = artifact(system, alice, {})
    assert alice.request('GET', '/api/v1/assets/' + identifier + '/content').status_code == 200


def test_forged_provenance_does_not_rewrite_canonical_source_links(system, alice):
    original = source(alice)
    derived = artifact(system, alice, {original: 1})
    with system[2]() as db:
        row = db.get(Record, derived)
        value = system[0].state.vault.open(row.payload, alice.user_id + ':record:' + derived)
        value['provenance']['source_versions'] = {}
        row.payload = system[0].state.vault.seal(value, alice.user_id + ':record:' + derived)
        db.get(Record, original).deleted = True
        db.commit()
    assert alice.request('GET', '/api/v1/data/' + derived).status_code == 404


@pytest.mark.skipif(os.getenv('HOMEAI_ARTIFACT_SOURCES_LIVE') != '1', reason='需显式启用隔离 homeai_test 的 PostgreSQL 来源与 RLS 验证')
@pytest.mark.parametrize('change', ['delete', 'unshare'])
def test_postgresql_shared_sources_revocation_and_canonical_rls(tmp_path, change):
    from fastapi.testclient import TestClient
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from homeai.api import create_app
    from homeai.config import Settings
    from homeai.crypto import Vault
    settings = Settings()
    settings.database_url = settings.database_url.rsplit('/', 1)[0] + '/homeai_test'
    settings.state_dir, settings.direct_enabled = tmp_path, False
    app = create_app(settings, vault=Vault(os.urandom(32)))
    client = TestClient(app)
    household, outside_household = 'qa-artifact-' + uuid.uuid4().hex, 'qa-artifact-other-' + uuid.uuid4().hex
    admin = SignedClient(client, app.state.db, household=household, role='infrastructure_owner')
    bob = SignedClient(client, app.state.db, household=household)
    outsider = SignedClient(client, app.state.db, household=outside_household, role='infrastructure_owner')
    try:
        original = source(admin)
        derived = artifact((app, client, app.state.db), admin, {original: 1}, household=household)
        outside_source = source(outsider)
        for identifier in (original, derived):
            assert admin.request('PUT', '/api/v1/data/' + identifier + '/visibility', {'visibility': 'family'}).status_code == 200
        assert bob.request('GET', '/api/v1/assets/' + derived + '/content').content == b'10\n'
        assert outsider.request('GET', '/api/v1/assets/' + derived + '/content').status_code == 404
        with app.state.db() as db:
            flags = db.execute(text('SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user')).one()
            assert not flags.rolsuper and not flags.rolbypassrls
            rls = db.execute(text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid='artifact_sources'::regclass")).one()
            assert rls.relrowsecurity and rls.relforcerowsecurity
            scope(db, bob.user_id, household)
            assert db.scalar(select(ArtifactSource.id).where(ArtifactSource.record_id == derived))
            db.rollback()
        # 外家庭管理员不能把自己的身份写成目标成果的归属，也不能伪造家庭上下文。
        for connection_household, row_household in [(outside_household, outside_household), (household, household)]:
            with app.state.db() as db:
                scope(db, outsider.user_id, connection_household)
                assert not db.scalar(select(ArtifactSource.id).where(ArtifactSource.record_id == derived))
                db.add(ArtifactSource(record_id=derived, source_id=outside_source, source_version=1,
                    owner_id=outsider.user_id, household_id=row_household))
                with pytest.raises(DBAPIError) as error:
                    db.flush()
                assert error.value.orig.sqlstate == '42501'
                db.rollback()
        if change == 'delete':
            assert admin.request('DELETE', '/api/v1/data/' + original).status_code == 200
            assert admin.request('GET', '/api/v1/assets/' + derived + '/content').status_code == 404
        else:
            assert admin.request('PUT', '/api/v1/data/' + original + '/visibility', {'visibility': 'personal'}).status_code == 200
            assert admin.request('GET', '/api/v1/assets/' + derived + '/content').status_code == 200
        assert bob.request('GET', '/api/v1/assets/' + derived + '/content').status_code == 404
    finally:
        for user in (admin, bob, outsider):
            user.request('DELETE', '/api/v1/devices/' + user.device_id)
        client.close()
        app.state.db.kw['bind'].dispose()
