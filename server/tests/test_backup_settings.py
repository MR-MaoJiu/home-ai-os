"""仅使用独立临时目录验证配置与调度；不连接数据库或执行真实备份。"""
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from homeai import backup_settings as backups
from homeai.config import Settings
from homeai.crypto import Vault
from homeai.security import Actor, authenticate


@pytest.fixture
def app(tmp_path):
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    return SimpleNamespace(settings=Settings(state_dir=state, database_url='postgresql+psycopg://unit@127.0.0.1:55432/homeai_unit'), vault=Vault(b'k' * 32))


def settings(app, tmp_path, **values):
    return backups.BackupSettingsInput(directory=str(tmp_path / 'archive'), **values)


def test_default_disabled_does_not_generate_keys_or_backups(app, monkeypatch):
    monkeypatch.setattr(backups, 'run_command', lambda _: pytest.fail('默认禁用不得执行备份'))
    store = backups.Store(app)
    assert store.view()['enabled'] is False
    assert store.view()['frequency'] == 'manual'
    assert store.view()['directory'] == ''
    backups.cycle(app)
    assert not store.path.exists()
    assert not store.key.exists()


def test_config_is_encrypted_private_and_does_not_run_on_save(app, tmp_path, monkeypatch):
    monkeypatch.setattr(backups, 'run_command', lambda _: pytest.fail('保存设置不得执行备份'))
    store = backups.Store(app)
    result = store.save(settings(app, tmp_path, enabled=True, frequency='daily'), 'operator')
    assert result['key_ready']
    assert result['next_run_at'] > backups.time.time() + 86000
    assert str(tmp_path / 'archive') not in store.path.read_text()
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.key.stat().st_mode & 0o777 == 0o600
    assert len(store.key.read_bytes()) == 32
    backups.cycle(app)
    assert not (tmp_path / 'archive').exists()
    store.save(settings(app, tmp_path, enabled=False, frequency='daily'), 'operator')
    assert store.read()['next_run_at'] is None
    backups.cycle(app)


def test_manual_request_runs_only_fixed_commands_and_preserves_existing_files(app, tmp_path, monkeypatch):
    store = backups.Store(app)
    store.save(settings(app, tmp_path), 'operator')
    output_dir = tmp_path / 'archive'
    output_dir.mkdir()
    existing = output_dir / 'existing.haib'
    existing.write_bytes(b'existing backup')
    queued = store.request_run('operator')
    assert queued['last_run']['status'] == 'queued'
    assert '_directory' not in queued['last_run']
    commands = []
    def invoke(arguments):
        commands.append(arguments)
        assert arguments[1] == str(backups.ROOT / 'scripts/backup.py')
        assert arguments[3] == '--key' and arguments[5] == '--file'
        if arguments[2] == 'create':
            Path(arguments[6]).write_bytes(b'unit test artifact')
    monkeypatch.setattr(backups, 'run_command', invoke)
    backups.cycle(app)
    result = store.view()
    assert [item[2] for item in commands] == ['create', 'verify']
    assert result['last_run']['status'] == 'succeeded'
    assert result['next_run_at'] is None
    assert existing.read_bytes() == b'existing backup'
    backups.cycle(app)
    assert len(commands) == 2


def test_disabled_schedules_and_interrupted_runs_do_not_retry_blindly(app, tmp_path, monkeypatch):
    store = backups.Store(app)
    store.save(settings(app, tmp_path, enabled=True, frequency='interval', interval_hours=2), 'operator')
    queued = store.request_run('operator')
    value = store.read()
    value['last_run']['status'] = 'running'
    value['next_run_at'] = backups.time.time() - 100
    with store.lock():
        store.write(value)
    monkeypatch.setattr(backups, 'run_command', lambda _: pytest.fail('中断任务不得自动重跑'))
    backups.cycle(app)
    value = store.read()
    assert value['last_run']['id'] == queued['last_run']['id']
    assert value['last_run']['status'] == 'failed'
    assert value['next_run_at'] > backups.time.time() + 7100
    backups.cycle(app)


def test_failure_is_visible_without_subprocess_secrets(app, tmp_path, monkeypatch):
    store = backups.Store(app)
    store.save(settings(app, tmp_path), 'operator')
    store.request_run('operator')
    def fail(arguments):
        raise subprocess.CalledProcessError(1, arguments, stderr=b'private-password-do-not-expose')
    monkeypatch.setattr(backups, 'run_command', fail)
    backups.cycle(app)
    result = store.view()
    assert result['last_run']['status'] == 'failed'
    assert 'private-password' not in str(result)


def test_invalid_paths_and_existing_key_are_not_overwritten(app, tmp_path):
    with pytest.raises(ValidationError):
        backups.BackupSettingsInput(enabled=True)
    with pytest.raises(ValidationError):
        backups.BackupSettingsInput(directory='../../outside')
    with pytest.raises(ValidationError):
        backups.BackupSettingsInput(interval_hours=0)
    linked = tmp_path / 'linked'
    linked.symlink_to(tmp_path / 'archive')
    with pytest.raises(ValueError):
        backups.Store(app).save(backups.BackupSettingsInput(directory=str(linked)), 'operator')
    store = backups.Store(app)
    store.key.write_bytes(b'wrong-key')
    with pytest.raises(ValueError):
        store.save(settings(app, tmp_path, enabled=True), 'operator')
    assert store.key.read_bytes() == b'wrong-key'


def test_management_routes_require_infrastructure_owner_and_do_not_trigger_run(app, tmp_path):
    api = FastAPI()
    api.state.settings, api.state.vault = app.settings, app.vault
    api.include_router(backups.router)
    actor = Actor('member', 'home', 'device', 'adult')
    api.dependency_overrides[authenticate] = lambda: actor
    client = TestClient(api)
    assert client.get('/api/v1/manage/backup-settings').status_code == 403
    actor = Actor('operator', 'home', 'device', 'infrastructure_owner')
    assert client.get('/api/v1/manage/backup-settings').json()['enabled'] is False
    assert client.post('/api/v1/manage/backup-settings/run').status_code == 422
    result = client.put('/api/v1/manage/backup-settings', json=settings(app, tmp_path).model_dump())
    assert result.status_code == 200
    assert result.json()['last_run'] is None
    assert client.post('/api/v1/manage/backup-settings/run').status_code == 202
    assert client.post('/api/v1/manage/backup-settings/run').status_code == 409


def test_restore_keeps_configuration_but_disables_schedule_and_pending_execution(app, tmp_path):
    store = backups.Store(app)
    store.save(settings(app, tmp_path, enabled=True, frequency='weekly'), 'operator')
    store.request_run('operator')
    restored = app.vault.open(backups.suspend_restored_configuration(store.path.read_text(), app.vault), backups.CONTEXT)
    assert restored['config']['directory'] == str(tmp_path / 'archive')
    assert restored['config']['enabled'] is False
    assert restored['next_run_at'] is None
    assert restored['last_run']['status'] == 'failed'


def test_operational_configs_are_supported_by_archive_verification_and_safe_extract(tmp_path):
    import hashlib
    import io
    import json
    import tarfile
    from homeai.backup import safe_extract, verify_archive
    values = {'database.dump': b'isolated unit fixture', 'notifications/apns.enc': b'encrypted push fixture', 'backup-settings.enc': b'encrypted backup fixture'}
    manifest = {'format_version': 2, 'files': {name: {'bytes': len(value), 'sha256': hashlib.sha256(value).hexdigest()} for name, value in values.items()}}
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w:gz') as output:
        for name, value in {**values, 'manifest.json': json.dumps(manifest).encode()}.items():
            info = tarfile.TarInfo(name); info.size = len(value); info.mode = 0o600
            output.addfile(info, io.BytesIO(value))
    assert verify_archive(archive)['manifest_verified'] is True
    archive.seek(0)
    destination = tmp_path / 'restore'
    safe_extract(archive, destination)
    assert (destination / 'notifications/apns.enc').read_bytes() == values['notifications/apns.enc']
    assert (destination / 'backup-settings.enc').stat().st_mode & 0o777 == 0o600


def test_backup_source_rejects_other_database_servers():
    from homeai.backup import development_database_name
    assert development_database_name('postgresql+psycopg://unit@127.0.0.1:55432/custom_home') == 'custom_home'
    for url in ['postgresql://unit@other-host:55432/homeai', 'postgresql://unit@127.0.0.1:5432/homeai', 'sqlite:///some.db']:
        with pytest.raises(ValueError):
            development_database_name(url)


@pytest.mark.parametrize('source_directory',['blobs','media','acme','tls','notifications'])
def test_backup_destination_cannot_recursively_include_itself(system,alice,source_directory):
    destination=system[0].state.settings.state_dir/source_directory/'backups'
    response=alice.request('PUT','/api/v1/manage/backup-settings',{'enabled':True,'directory':str(destination),'frequency':'daily'})
    assert response.status_code==422
    assert not destination.exists()
