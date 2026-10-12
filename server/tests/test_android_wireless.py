"""无线设备身份、私网边界、配置合并与固定 transport；只使用公开元数据替身。"""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from homeai import android_adb as adb, android_wireless as wireless

SERIAL = 'PUBLICDEVICE123'
ENDPOINT = '192.168.1.25:5555'
PROPS = [SERIAL, 'public_product', 'public_vendor']
FINGERPRINT = hashlib.sha256(json.dumps(PROPS, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def policy(*, prefer=False, enabled=True):
    return adb.AndroidPolicy(enabled=enabled, adb_binary='/usr/bin/true', serials=[SERIAL], ime_serials=[SERIAL],
        wireless=[adb.WirelessBinding(serial=SERIAL, endpoint=ENDPOINT, mode='legacy_tcp',
            identity_sha256=FINGERPRINT, prefer_wireless=prefer)])


@pytest.mark.parametrize('endpoint', ['8.8.8.8:5555', '127.0.0.1:5555', '0.0.0.0:1', 'example.com:5555',
    '192.168.1.2:0', '192.168.1.2:65536', '192.168.1.2:5555;id', '[::1]:5555', '[fe80::1%en0]:5555'])
def test_wireless_endpoint_rejects_public_dns_loopback_or_invalid_port(endpoint):
    with pytest.raises(ValueError): adb.private_endpoint(endpoint)


def test_private_endpoint_and_policy_keep_canonical_separate():
    assert adb.private_endpoint('192.168.1.25:5555') == ENDPOINT
    assert adb.private_endpoint('[fd00::25]:5555') == '[fd00::25]:5555'
    assert policy().serials == [SERIAL] and policy().wireless[0].endpoint == ENDPOINT
    with pytest.raises(ValidationError):
        adb.AndroidPolicy(enabled=True, adb_binary='/usr/bin/true', serials=['another'], wireless=policy().wireless)


@pytest.mark.asyncio
@pytest.mark.parametrize('prefer,expected', [(False, 7), (True, 9)])
async def test_resolver_selects_online_usb_or_explicit_wireless_preference(monkeypatch, prefer, expected):
    listing = [{'selector': SERIAL, 'state': 'device', 'transport_id': 7},
               {'selector': ENDPOINT, 'state': 'device', 'transport_id': 9}]
    monkeypatch.setattr(wireless, 'inventory', AsyncMock(return_value=listing))
    fingerprint = AsyncMock(return_value=FINGERPRINT)
    monkeypatch.setattr(wireless, 'hardware_identity', fingerprint)
    prefix = await adb.command_prefix(policy(prefer=prefer), SERIAL)
    assert prefix == ['/usr/bin/true', '-t', str(expected)]
    assert fingerprint.call_args.args[1] == expected


@pytest.mark.asyncio
async def test_unregistered_endpoint_and_reused_ip_cannot_execute(monkeypatch):
    monkeypatch.setattr(wireless, 'inventory', AsyncMock(return_value=[
        {'selector': ENDPOINT, 'state': 'device', 'transport_id': 9},
        {'selector': '192.168.1.66:5555', 'state': 'device', 'transport_id': 10}]))
    identity = AsyncMock(return_value='0' * 64)
    monkeypatch.setattr(wireless, 'hardware_identity', identity)
    with pytest.raises(HTTPException) as error:
        await adb.command_prefix(policy(), SERIAL)
    assert error.value.status_code == 403
    assert identity.await_count == 1 and identity.call_args.args[1] == 9


@pytest.mark.asyncio
async def test_side_effect_failure_is_not_retried_on_another_transport(monkeypatch):
    monkeypatch.setattr(adb, 'command_prefix', AsyncMock(return_value=['/usr/bin/true', '-t', '9']))
    operation = AsyncMock(side_effect=HTTPException(504, '公开测试连接中断'))
    monkeypatch.setattr(adb, '_execute_argv', operation)
    with pytest.raises(HTTPException):
        await adb._run(policy(), SERIAL, ['shell', 'input', 'keyevent', '3'])
    assert operation.await_count == 1
    assert operation.call_args.args[0] == ['/usr/bin/true', '-t', '9', 'shell', 'input', 'keyevent', '3']


@pytest.mark.asyncio
async def test_hardware_identity_rejects_mismatched_serial_without_storing_properties(monkeypatch):
    command = AsyncMock(side_effect=[b'OTHER_DEVICE', b'public_product', b'public_vendor'])
    monkeypatch.setattr(wireless, '_transport', command)
    with pytest.raises(HTTPException) as error:
        await wireless.hardware_identity(policy(), 9, canonical_serial=SERIAL)
    assert error.value.status_code == 403
    command.side_effect = [value.encode() for value in PROPS]
    assert await wireless.hardware_identity(policy(), 9, canonical_serial=SERIAL) == FINGERPRINT


def write_policy(tmp_path, value):
    path = tmp_path / 'android-policy.json'
    path.write_text(value.model_dump_json())
    path.chmod(0o600)
    return SimpleNamespace(state_dir=tmp_path)


def test_atomic_update_preserves_other_devices_input_methods_and_wireless(tmp_path):
    settings = write_policy(tmp_path, policy())
    def add(current):
        data = current.model_dump(mode='json')
        data['serials'].append('SECOND_PUBLIC_DEVICE')
        return data
    result = wireless.update_policy(settings, add)
    assert result.serials == [SERIAL, 'SECOND_PUBLIC_DEVICE']
    assert result.ime_serials == [SERIAL]
    assert result.wireless == policy().wireless
    assert (tmp_path / 'android-policy.json').stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob('.android-policy-*'))


@pytest.mark.asyncio
async def test_pair_code_is_only_stdin_and_pair_port_is_distinct(tmp_path, monkeypatch):
    base = policy().model_copy(update={'wireless': []})
    settings = write_policy(tmp_path, base)
    host = AsyncMock(return_value=b'Successfully paired to public address')
    monkeypatch.setattr(wireless, '_host', host)
    monkeypatch.setattr(wireless, '_explicit_connect', AsyncMock(return_value={'transport_id': 9}))
    monkeypatch.setattr(wireless, '_transport', AsyncMock(side_effect=[b'30', b'41234']))
    monkeypatch.setattr(wireless, 'hardware_identity', AsyncMock(return_value=FINGERPRINT))
    result = await wireless.pair_wireless(settings, SERIAL, '192.168.1.25:37123',
        '192.168.1.25:41234', '123456', prefer_wireless=True)
    assert result['security'] == 'tls'
    assert host.call_args.args[1] == ['pair', '192.168.1.25:37123']
    assert host.call_args.kwargs['stdin_data'] == b'123456\n'
    assert '123456' not in str(host.call_args.args)
    saved = adb.load_policy(settings)
    assert saved.wireless[0].endpoint == '192.168.1.25:41234'
    assert saved.wireless[0].prefer_wireless
    assert '123456' not in (tmp_path / 'android-policy.json').read_text()
    assert saved.serials == [SERIAL] and saved.ime_serials == [SERIAL]


@pytest.mark.asyncio
async def test_legacy_wireless_requires_local_acceptance_and_authorized_usb(tmp_path, monkeypatch):
    settings = write_policy(tmp_path, policy().model_copy(update={'wireless': []}))
    host = AsyncMock()
    monkeypatch.setattr(wireless, '_host', host)
    with pytest.raises(HTTPException) as refusal:
        await wireless.enable_legacy(settings, SERIAL, ENDPOINT)
    assert refusal.value.status_code == 409 and host.await_count == 0
    monkeypatch.setattr(wireless, 'inventory', AsyncMock(return_value=[]))
    with pytest.raises(HTTPException):
        await wireless.enable_legacy(settings, SERIAL, ENDPOINT, allow_legacy_tcp=True)


@pytest.mark.asyncio
async def test_disable_preserves_member_serial_and_restores_usb_option(tmp_path, monkeypatch):
    settings = write_policy(tmp_path, policy(prefer=True))
    monkeypatch.setattr(wireless, 'resolve_target', AsyncMock(return_value={'transport_id': 9}))
    command = AsyncMock(return_value=b'restarting in USB mode')
    monkeypatch.setattr(wireless, '_transport', command)
    disconnect = AsyncMock(return_value=b'disconnected')
    monkeypatch.setattr(wireless, '_host', disconnect)
    result = await wireless.disable_wireless(settings, SERIAL)
    assert result['wireless_enabled'] is False and result['device_listener_disabled'] is True
    assert command.call_args.args[2] == ['usb']
    assert disconnect.call_args.args[1] == ['disconnect', ENDPOINT]
    saved = adb.load_policy(settings)
    assert saved.serials == [SERIAL] and saved.ime_serials == [SERIAL]
    assert not saved.wireless[0].enabled


@pytest.mark.asyncio
async def test_inventory_reports_transport_without_replacing_serial(monkeypatch):
    monkeypatch.setattr(wireless, 'inventory', AsyncMock(return_value=[{'selector': ENDPOINT,
        'state': 'device', 'transport_id': 9}]))
    monkeypatch.setattr(wireless, 'hardware_identity', AsyncMock(return_value=FINGERPRINT))
    row = (await wireless.device_statuses(policy()))[0]
    assert row['serial'] == SERIAL and row['transport'] == 'wireless'
    assert row['wireless_configured'] and row['status'] == 'online'


def test_late_connect_cannot_restore_concurrently_revoked_wireless(tmp_path):
    original = policy()
    settings = write_policy(tmp_path, original)
    revoked = original.wireless[0].model_copy(update={'enabled': False})
    wireless._save(settings, original, revoked)
    with pytest.raises(HTTPException) as conflict:
        wireless._save(settings, original, original.wireless[0])
    assert conflict.value.status_code == 409
    assert not adb.load_policy(settings).wireless[0].enabled


def test_wireless_revoke_remains_possible_when_global_executor_disabled(tmp_path):
    original = policy(enabled=False)
    settings = write_policy(tmp_path, original)
    wireless._save(settings, original, original.wireless[0].model_copy(update={'enabled': False}))
    assert not adb.load_policy(settings).wireless[0].enabled


def test_existing_registration_cli_incrementally_preserves_wireless(tmp_path):
    import os
    import subprocess
    import sys
    settings = write_policy(tmp_path, policy(prefer=True))
    environment = dict(os.environ)
    environment['PYTHONPATH'] = 'server'
    completed = subprocess.run([sys.executable, '-m', 'homeai.android_adb', '--state-dir', str(tmp_path),
        '--adb', '/usr/bin/true', '--serial', 'SECOND_PUBLIC_DEVICE'], capture_output=True, text=True,
        env=environment, check=True)
    assert json.loads(completed.stdout)['registered_devices'] == 2
    result = adb.load_policy(settings)
    assert result.wireless == policy(prefer=True).wireless
    assert result.ime_serials == [SERIAL]


@pytest.mark.asyncio
async def test_tls_registration_cannot_label_legacy_port_as_encrypted(monkeypatch):
    command = AsyncMock(return_value=b'41234')
    monkeypatch.setattr(wireless, '_transport', command)
    with pytest.raises(HTTPException) as mismatch:
        await wireless.verify_tls_port(policy(), 9, ENDPOINT)
    assert mismatch.value.status_code == 403
    await wireless.verify_tls_port(policy(), 9, '192.168.1.25:41234')


@pytest.mark.asyncio
async def test_explicit_connect_waits_for_same_endpoint_listener(monkeypatch):
    host = AsyncMock(side_effect=[b'failed to connect: Connection refused', b'connected to public'])
    listing = AsyncMock(side_effect=[[], [{'selector': ENDPOINT, 'state': 'device', 'transport_id': 9}]])
    monkeypatch.setattr(wireless, '_host', host)
    monkeypatch.setattr(wireless, 'inventory', listing)
    monkeypatch.setattr(wireless, 'CONNECT_RETRY_INTERVAL', .001)
    result = await wireless._explicit_connect(policy(), ENDPOINT)
    assert result['transport_id'] == 9
    assert host.await_count == 2
    assert all(call.args[1] == ['connect', ENDPOINT] for call in host.call_args_list)


@pytest.mark.asyncio
async def test_explicit_connect_failure_has_bounded_wait(monkeypatch):
    host = AsyncMock(side_effect=HTTPException(502, '公开测试未监听'))
    monkeypatch.setattr(wireless, '_host', host)
    monkeypatch.setattr(wireless, 'CONNECT_WAIT_SECONDS', .025)
    monkeypatch.setattr(wireless, 'CONNECT_RETRY_INTERVAL', .005)
    with pytest.raises(HTTPException) as error:
        await wireless._explicit_connect(policy(), ENDPOINT)
    assert error.value.status_code == 503
    assert 2 <= host.await_count <= 8
    assert all(call.args[1] == ['connect', ENDPOINT] for call in host.call_args_list)


@pytest.mark.asyncio
async def test_existing_tcp_listener_registers_without_restarting_adbd(tmp_path, monkeypatch):
    settings = write_policy(tmp_path, policy().model_copy(update={'wireless': []}))
    monkeypatch.setattr(wireless, 'inventory', AsyncMock(return_value=[
        {'selector': SERIAL, 'state': 'device', 'transport_id': 7}]))
    monkeypatch.setattr(wireless, 'hardware_identity', AsyncMock(return_value=FINGERPRINT))
    transport = AsyncMock(return_value=b'5555')
    monkeypatch.setattr(wireless, '_transport', transport)
    monkeypatch.setattr(wireless, '_explicit_connect', AsyncMock(return_value={'transport_id': 9}))
    result = await wireless.enable_legacy(settings, SERIAL, ENDPOINT,
        allow_legacy_tcp=True, prefer_wireless=True)
    assert result['listener_restarted'] is False
    assert transport.await_count == 1
    assert transport.call_args.args[2] == ['shell', 'getprop', 'service.adb.tcp.port']
    assert adb.load_policy(settings).wireless[0].prefer_wireless


@pytest.mark.asyncio
@pytest.mark.parametrize('restored', [True, False])
async def test_changed_listener_failure_rolls_back_and_reports_result(tmp_path, monkeypatch, restored):
    settings = write_policy(tmp_path, policy().model_copy(update={'wireless': []}))
    monkeypatch.setattr(wireless, 'inventory', AsyncMock(return_value=[
        {'selector': SERIAL, 'state': 'device', 'transport_id': 7}]))
    monkeypatch.setattr(wireless, 'hardware_identity', AsyncMock(return_value=FINGERPRINT))
    transport = AsyncMock(side_effect=[b'0', b'restarting in TCP mode'])
    monkeypatch.setattr(wireless, '_transport', transport)
    monkeypatch.setattr(wireless, '_explicit_connect', AsyncMock(side_effect=HTTPException(503, '公开测试监听失败')))
    monkeypatch.setattr(wireless, '_host', AsyncMock(return_value=b'disconnected'))
    restore = AsyncMock(return_value=restored)
    monkeypatch.setattr(wireless, '_restore_usb', restore)
    with pytest.raises(HTTPException) as error:
        await wireless.enable_legacy(settings, SERIAL, ENDPOINT, allow_legacy_tcp=True)
    assert restore.await_count == 1
    assert ('已核实恢复 USB' if restored else '未能确认恢复 USB') in error.value.detail
    assert '本次无线配置未保存' in error.value.detail
    assert transport.call_args_list[-1].args[2] == ['tcpip', '5555']
    assert adb.load_policy(settings).wireless == []


@pytest.mark.asyncio
async def test_unchanged_listener_failure_does_not_disable_existing_listener(tmp_path, monkeypatch):
    settings = write_policy(tmp_path, policy().model_copy(update={'wireless': []}))
    monkeypatch.setattr(wireless, 'inventory', AsyncMock(return_value=[
        {'selector': SERIAL, 'state': 'device', 'transport_id': 7}]))
    monkeypatch.setattr(wireless, 'hardware_identity', AsyncMock(return_value=FINGERPRINT))
    monkeypatch.setattr(wireless, '_transport', AsyncMock(return_value=b'5555'))
    monkeypatch.setattr(wireless, '_explicit_connect', AsyncMock(side_effect=HTTPException(503, '公开测试不可达')))
    monkeypatch.setattr(wireless, '_host', AsyncMock(return_value=b'disconnected'))
    restore = AsyncMock(return_value=True)
    monkeypatch.setattr(wireless, '_restore_usb', restore)
    with pytest.raises(HTTPException) as error:
        await wireless.enable_legacy(settings, SERIAL, ENDPOINT, allow_legacy_tcp=True)
    assert restore.await_count == 0 and '原监听保持不变' in error.value.detail


@pytest.mark.asyncio
async def test_restore_usb_checks_identity_and_closed_port_without_repeating_command(monkeypatch):
    monkeypatch.setattr(wireless, 'inventory', AsyncMock(return_value=[
        {'selector': SERIAL, 'state': 'device', 'transport_id': 7}]))
    monkeypatch.setattr(wireless, 'hardware_identity', AsyncMock(return_value=FINGERPRINT))
    transport = AsyncMock(side_effect=[HTTPException(502, '公开测试重启中断回执'), b'-1'])
    monkeypatch.setattr(wireless, '_transport', transport)
    assert await wireless._restore_usb(policy(), SERIAL, FINGERPRINT) is True
    assert sum(call.args[2] == ['usb'] for call in transport.call_args_list) == 1
    assert transport.call_args.args[2] == ['shell', 'getprop', 'service.adb.tcp.port']
