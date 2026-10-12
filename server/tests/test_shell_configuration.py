import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from homeai.security import Actor
from homeai.shell_configuration import HostAction, ShellPolicy, fingerprint, host_action, load_policy, main, status


def settings(tmp_path):
    return SimpleNamespace(shell_policy_file=tmp_path / 'shell-policy.json', state_dir=tmp_path, environment='development')


def write_policy(tmp_path, **overrides):
    config = settings(tmp_path)
    data = ShellPolicy(host_members=['admin']).model_dump(mode='json') | overrides
    config.shell_policy_file.write_text(json.dumps(data))
    config.shell_policy_file.chmod(0o600)
    return config


def action(tmp_path, **overrides):
    return dict(id='system-info', title='系统信息', description='只读检查', script='uname -s', cwd=str(tmp_path), timeout_seconds=30) | overrides


def administrator():
    return Actor('admin', 'home', 'device', 'infrastructure_owner')


def test_missing_policy_disables_all_execution(tmp_path):
    config = settings(tmp_path)
    policy = load_policy(config)
    assert not policy.host_enabled and not policy.sandbox_enabled
    assert not config.shell_policy_file.exists()


@pytest.mark.parametrize('permissions', [0o644, 0o666, 0o640, 0o400, 0o700])
def test_policy_requires_owner_read_write_only(tmp_path, permissions):
    config = write_policy(tmp_path)
    config.shell_policy_file.chmod(permissions)
    with pytest.raises(HTTPException, match='0600'):
        load_policy(config)


def test_policy_rejects_symbolic_link_and_fifo(tmp_path):
    config = write_policy(tmp_path)
    target = tmp_path / 'target.json'
    config.shell_policy_file.rename(target)
    config.shell_policy_file.symlink_to(target)
    with pytest.raises(HTTPException):
        load_policy(config)
    config.shell_policy_file.unlink()
    os.mkfifo(config.shell_policy_file, 0o600)
    with pytest.raises(HTTPException):
        load_policy(config)


@pytest.mark.parametrize('raw', ['{"version":1,"host_enabled":true,"host_enabled":false}',
                               '{"version":1,"sudo":true}', '{"version":2}', '[]', '\ufffd'])
def test_policy_rejects_ambiguous_or_unknown_schema(tmp_path, raw):
    config = write_policy(tmp_path)
    config.shell_policy_file.write_text(raw)
    with pytest.raises(HTTPException, match='不符合契约'):
        load_policy(config)


@pytest.mark.parametrize('image', ['python:latest', 'python:3.12', 'sha256:abc', 'repository@sha256:' + 'g' * 64])
def test_mutable_or_invalid_sandbox_images_are_rejected(image):
    with pytest.raises(ValidationError):
        ShellPolicy(sandbox_image=image)


@pytest.mark.parametrize('image', ['sha256:' + 'a' * 64, 'registry.example:5000/image@sha256:' + 'b' * 64])
def test_immutable_images_are_accepted(image):
    assert ShellPolicy(sandbox_image=image, sandbox_enabled=True).sandbox_enabled


@pytest.mark.parametrize('change', [{'timeout_seconds': 301}, {'timeout_seconds': True}, {'cwd': 'relative'},
                                  {'script': ' '}, {'script': 'echo\x00oops'}, {'id': '--flag'}])
def test_host_action_has_bounded_fixed_fields(tmp_path, change):
    with pytest.raises(ValidationError):
        HostAction(**action(tmp_path, **change))


def test_duplicate_actions_are_rejected(tmp_path):
    with pytest.raises(ValidationError):
        ShellPolicy(host_actions=[HostAction(**action(tmp_path)), HostAction(**action(tmp_path))])


def test_only_infrastructure_owner_can_resolve_host_action(tmp_path):
    config = write_policy(tmp_path, host_enabled=True, host_actions=[action(tmp_path)])
    for role in ['adult', 'child', 'service', 'agent']:
        with pytest.raises(HTTPException) as error:
            host_action(config, Actor('member', 'home', 'device', role), 'system-info')
        assert error.value.status_code == 403
    resolved = host_action(config, administrator(), 'system-info')
    assert resolved['script'] == 'uname -s'
    assert resolved['fingerprint'] == resolved['action_fingerprint']


def test_policy_update_revokes_previous_fingerprint(tmp_path):
    config = write_policy(tmp_path, host_enabled=True, host_actions=[action(tmp_path)])
    before = host_action(config, administrator(), 'system-info')
    write_policy(tmp_path, host_enabled=True, host_actions=[action(tmp_path, script='uname -r')])
    after = host_action(config, administrator(), 'system-info')
    assert before['action_fingerprint'] != after['action_fingerprint']
    assert before['policy_fingerprint'] != after['policy_fingerprint']
    write_policy(tmp_path, host_enabled=False, host_actions=[action(tmp_path)])
    with pytest.raises(HTTPException, match='尚未'):
        host_action(config, administrator(), 'system-info')


def test_unknown_action_or_missing_fixed_directory_fail_before_execution(tmp_path):
    config = write_policy(tmp_path, host_enabled=True, host_actions=[action(tmp_path, cwd=str(tmp_path / 'missing'))])
    with pytest.raises(HTTPException, match='未登记'):
        host_action(config, administrator(), 'arbitrary')
    with pytest.raises(HTTPException, match='目录不可用'):
        host_action(config, administrator(), 'system-info')


def test_status_does_not_disclose_host_scripts_or_members_to_ordinary_member(tmp_path):
    config = write_policy(tmp_path, host_enabled=True, host_actions=[action(tmp_path)],
                          sandbox_enabled=True, sandbox_image='sha256:' + 'a' * 64, sandbox_members=['member', 'other'])
    visible = status(config, Actor('member', 'home', 'device', 'adult'), include_scripts=True)
    assert visible['sandbox']['ready']
    assert visible['sandbox']['members'] == []
    assert visible['host']['actions'] == []
    assert not visible['host']['enabled']
    assert visible['policy_file'] == ''
    redacted = status(config, administrator())
    assert 'script' not in redacted['host']['actions'][0]
    disclosed = status(config, administrator(), include_scripts=True)
    assert disclosed['host']['actions'][0]['script'] == 'uname -s'


def test_production_requires_runsc_and_explicit_sandbox_member(tmp_path):
    config = write_policy(tmp_path, sandbox_enabled=True, sandbox_image='sha256:' + 'a' * 64, sandbox_members=['admin'])
    config.environment = 'production'
    assert not status(config, administrator())['sandbox']['ready']
    write_policy(tmp_path, sandbox_enabled=True, sandbox_image='sha256:' + 'a' * 64,
                 sandbox_members=['admin'], sandbox_runtime='runsc')
    assert status(config, administrator())['sandbox']['ready']
    assert not status(config, Actor('other', 'home', 'device', 'infrastructure_owner'))['sandbox']['ready']


def test_cli_creates_updates_and_revokes_policy_without_implicit_host_enable(tmp_path):
    config = settings(tmp_path)
    args = ['--policy-file', str(config.shell_policy_file)]
    main(args + ['init'])
    assert config.shell_policy_file.stat().st_mode & 0o777 == 0o600
    with pytest.raises(SystemExit) as error:
        main(args + ['init'])
    assert error.value.code == 1
    script = tmp_path / 'health.sh'
    script.write_text('uname -s\n')
    main(args + ['add-host-action', '--id', 'system-info', '--title', '系统信息', '--script-file', str(script), '--cwd', str(tmp_path)])
    policy = load_policy(config)
    assert not policy.host_enabled and len(policy.host_actions) == 1
    main(args + ['enable-host', '--member', 'admin'])
    assert host_action(config, administrator(), 'system-info')['script'] == 'uname -s\n'
    main(args + ['enable-sandbox', '--image', 'sha256:' + 'a' * 64, '--member', 'admin'])
    assert load_policy(config).sandbox_enabled
    main(args + ['disable-sandbox'])
    main(args + ['remove-host-action', '--id', 'system-info'])
    main(args + ['disable-host'])
    policy = load_policy(config)
    assert not policy.host_actions and not policy.host_enabled and not policy.sandbox_enabled


def test_fingerprint_is_independent_of_json_key_order():
    assert fingerprint({'a': 1, 'b': 2}) == fingerprint({'b': 2, 'a': 1})


def test_cli_registers_effects_for_approval_and_missing_effects_remain_unknown(tmp_path):
    config = settings(tmp_path)
    script = tmp_path / 'maintenance.sh'; script.write_text('printf checked')
    main(['--policy-file', str(config.shell_policy_file), 'add-host-action', '--id', 'maintenance',
          '--title', '维护动作', '--script-file', str(script), '--cwd', str(tmp_path),
          '--impact-summary', '更新指定文件并重启测试服务', '--file-change', '/srv/test.conf：修改',
          '--service-change', 'test-service：重启'])
    registered = load_policy(config).host_actions[0]
    assert registered.impact_summary == '更新指定文件并重启测试服务'
    assert registered.file_changes == ['/srv/test.conf：修改']
    assert registered.service_changes == ['test-service：重启']
    legacy = HostAction(**action(tmp_path))
    assert legacy.impact_summary == '' and legacy.file_changes == [] and legacy.service_changes == []


@pytest.mark.parametrize('change', [{'impact_summary': '\0'}, {'file_changes': ['']},
                                  {'service_changes': ['x' * 501]}, {'file_changes': ['change'] * 33}])
def test_registered_impact_is_bounded_and_cannot_contain_empty_or_null_entries(tmp_path, change):
    with pytest.raises(ValidationError):
        HostAction(**action(tmp_path, **change))


def test_other_household_administrator_has_no_implicit_host_access(tmp_path):
    config = write_policy(tmp_path, host_enabled=True, host_actions=[action(tmp_path)])
    outsider = Actor('another-admin', 'another-home', 'another-device', 'infrastructure_owner')
    with pytest.raises(HTTPException) as error:
        host_action(config, outsider, 'system-info')
    assert error.value.status_code == 403
    assert status(config, outsider, include_scripts=True)['host']['actions'] == []


def test_explicit_null_policy_path_uses_own_state_directory(tmp_path):
    config = write_policy(tmp_path)
    config.shell_policy_file = None
    assert load_policy(config).host_members == ['admin']


def test_parallel_cli_registration_preserves_both_actions(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    config = settings(tmp_path)
    args = ['--policy-file', str(config.shell_policy_file)]
    main(args + ['init'])
    script = tmp_path / 'test.sh'
    script.write_text('printf test')

    def register(name):
        main(args + ['add-host-action', '--id', name, '--title', name, '--script-file', str(script), '--cwd', str(tmp_path)])

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(register, ['action-a', 'action-b']))
    assert {item.id for item in load_policy(config).host_actions} == {'action-a', 'action-b'}


def test_cli_rejects_symlink_lock_without_modifying_target(tmp_path):
    config = settings(tmp_path)
    target = tmp_path / 'untouched'
    target.write_text('preserve')
    (tmp_path / '.shell-policy.json.lock').symlink_to(target)
    with pytest.raises(SystemExit):
        main(['--policy-file', str(config.shell_policy_file), 'init'])
    assert target.read_text() == 'preserve'
    assert not config.shell_policy_file.exists()
