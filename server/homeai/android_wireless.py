"""本机显式管理无线 ADB；稳定成员设备标识与可变网络地址分离。"""
import argparse
import asyncio
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from types import SimpleNamespace

from fastapi import HTTPException
from .android_adb import (AndroidPolicy, WirelessBinding, load_policy, policy_path,
                          private_endpoint, _execute_argv)


CONNECT_WAIT_SECONDS = 12
CONNECT_RETRY_INTERVAL = .25
ROLLBACK_WAIT_SECONDS = 5


async def _host(policy, arguments, *, timeout=8, stdin_data=None):
    if not policy.enabled:
        raise HTTPException(409, '本机 Android 执行器尚未启用')
    return await _execute_argv([policy.adb_binary] + arguments, timeout=timeout, stdin_data=stdin_data)


async def _transport(policy, identifier, arguments, *, timeout=8):
    if type(identifier) is not int or identifier < 1:
        raise HTTPException(409, 'ADB 连接标识无效，请重新连接设备')
    return await _execute_argv([policy.adb_binary, '-t', str(identifier)] + arguments, timeout=timeout, limit=2048)


async def inventory(policy, *, timeout=8):
    output = await _host(policy, ['devices', '-l'], timeout=timeout)
    result = []
    for line in output.decode('utf-8', 'replace').splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] == 'List':
            continue
        metadata = dict(item.split(':', 1) for item in parts[2:] if ':' in item)
        try:
            transport_id = int(metadata['transport_id'])
        except (KeyError, ValueError):
            transport_id = None
        result.append({'selector': parts[0], 'state': parts[1], 'transport_id': transport_id})
    return result


async def hardware_identity(policy, transport_id, *, canonical_serial):
    """核对硬件属性以防 IP 被另一设备复用；旧版非 TLS ADB 不具备网络抗伪装保证。"""
    values = []
    for name in ('ro.serialno', 'ro.product.device', 'ro.product.manufacturer'):
        raw = await _transport(policy, transport_id, ['shell', 'getprop', name])
        value = raw.decode('utf-8', 'strict').strip()
        if not value or len(value) > 256 or '\0' in value or '\n' in value:
            raise HTTPException(409, '无法核对设备固定身份，请使用 USB 重新登记')
        values.append(value)
    if values[0] != canonical_serial:
        raise HTTPException(403, '无线地址对应的设备与原成员设备不一致，已拒绝连接')
    return hashlib.sha256(json.dumps(values, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()



async def verify_tls_port(policy, transport_id, endpoint):
    """配对端口不等于连接端口，也不能把同机传统 5555 连接标记为 TLS。"""
    value = await _transport(policy, transport_id, ['shell', 'getprop', 'service.adb.tls.port'])
    try:
        actual = int(value.strip())
    except ValueError:
        raise HTTPException(409, '设备未提供可核实的 TLS 调试连接端口') from None
    if actual != int(endpoint.rsplit(':', 1)[1]):
        raise HTTPException(403, '连接端口不是设备当前 TLS 调试端口，不能将传统 ADB 连接当作加密连接')

def _configured(policy, serial):
    if serial not in policy.serials:
        raise HTTPException(403, '稳定设备标识尚未由本机登记')
    return next((item for item in policy.wireless if item.serial == serial), None)


async def resolve_target(policy, serial, *, listing=None):
    if not policy.enabled:
        raise HTTPException(409, '本机 Android 执行器尚未启用')
    binding = _configured(policy, serial)
    entries = await inventory(policy) if listing is None else listing
    usb = next((item for item in entries if item['selector'] == serial and item['state'] == 'device'), None)
    wireless = next((item for item in entries if binding and binding.enabled
        and item['selector'] == binding.endpoint and item['state'] == 'device'), None)
    choices = [('wireless', wireless), ('usb', usb)] if binding and binding.enabled and binding.prefer_wireless else [('usb', usb), ('wireless', wireless)]
    failed_identity = False
    for transport, item in choices:
        if not item or item['transport_id'] is None:
            continue
        try:
            if binding:
                fingerprint = await hardware_identity(policy, item['transport_id'], canonical_serial=serial)
                if fingerprint != binding.identity_sha256:
                    failed_identity = True
                    continue
                if transport == 'wireless' and binding.mode == 'tls':
                    await verify_tls_port(policy, item['transport_id'], binding.endpoint)
        except HTTPException:
            failed_identity = True
            continue
        return {'canonical_serial': serial, 'selector': item['selector'], 'transport_id': item['transport_id'],
                'transport': transport, 'wireless_mode': binding.mode if transport == 'wireless' else None}
    if failed_identity:
        raise HTTPException(403, '已登记连接的设备身份无法确认，已阻止执行')
    raise HTTPException(409, '设备没有可用的已授权 USB 或无线连接，请在本机明确连接已登记地址')


async def device_statuses(policy):
    listed = await inventory(policy)
    result = []
    for serial in policy.serials:
        binding = _configured(policy, serial)
        selector_status = {item['selector']: item['state'] for item in listed}
        state = selector_status.get(serial)
        status = 'online' if state == 'device' else 'unauthorized' if state in ('unauthorized', 'authorizing', 'no') else 'offline'
        transport = 'usb' if status == 'online' else 'offline'
        if binding:
            try:
                selected = await resolve_target(policy, serial, listing=listed)
                status, transport = 'online', selected['transport']
            except HTTPException as error:
                status = 'unauthorized' if error.status_code == 403 or (binding.enabled and selector_status.get(binding.endpoint) in ('unauthorized', 'authorizing')) else 'offline'
                transport = 'offline'
        result.append({'serial': serial, 'label': serial, 'backend': 'emulator' if serial.startswith('emulator-') else 'physical',
            'status': status, 'transport': transport, 'wireless_configured': binding is not None,
            'wireless_enabled': bool(binding and binding.enabled),
            'prefer_wireless': bool(binding and binding.prefer_wireless),
            'wireless_security': binding.mode if binding else None})
    return result


def update_policy(settings, change):
    """本机配置增量修改用同一文件锁，避免输入法／设备登记互相覆盖。"""
    target = policy_path(settings)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = os.open(target.with_name(target.name + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    temporary = None
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = load_policy(settings)
        result = change(current)
        if not isinstance(result, AndroidPolicy):
            # python -m 启动时 __main__ 与包导入可能持有同契约的不同类对象。
            result = AndroidPolicy.model_validate(result.model_dump(mode='json') if hasattr(result, 'model_dump') else result)
        fd, temporary = tempfile.mkstemp(prefix='.android-policy-', dir=target.parent)
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(result.model_dump_json(indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        load_policy(SimpleNamespace(android_policy_file=temporary, state_dir=target.parent))
        os.replace(temporary, target)
        temporary = None
        return result
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)


def _save(settings, policy, binding):
    def mutate(current):
        # 网络操作期间另一次本机配置可能已经撤销该设备，不能恢复旧白名单。
        if binding.serial not in current.serials or (binding.enabled and not current.enabled) or current.adb_binary != policy.adb_binary:
            raise HTTPException(409, '本机设备配置已变化，请重新检查后再登记无线')
        previous = next((item for item in policy.wireless if item.serial == binding.serial), None)
        latest = next((item for item in current.wireless if item.serial == binding.serial), None)
        if previous != latest:
            raise HTTPException(409, '无线配置在连接过程中已被修改或撤销，不能覆盖新配置')
        values = current.model_dump(mode='json')
        values['wireless'] = [item.model_dump(mode='json') for item in current.wireless if item.serial != binding.serial] + [binding.model_dump(mode='json')]
        return values
    return update_policy(settings, mutate)


async def _explicit_connect(policy, endpoint):
    endpoint = private_endpoint(endpoint)
    deadline = time.monotonic() + CONNECT_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            # tcpip 返回时 adbd 可能尚在重启；只重试此明确地址的幂等建连，不重放设备操作。
            await _host(policy, ['connect', endpoint], timeout=min(2, max(.05, deadline - time.monotonic())))
            item = next((item for item in await inventory(policy, timeout=min(2, max(.05, deadline - time.monotonic()))) if item['selector'] == endpoint
                and item['state'] == 'device' and item['transport_id']), None)
            if item:
                return item
        except HTTPException as error:
            if error.status_code not in (502, 503, 504):
                raise
        await asyncio.sleep(min(CONNECT_RETRY_INTERVAL, max(0, deadline - time.monotonic())))
    raise HTTPException(503, '无线 ADB 在等待时间内未完成连接，请核对监听地址、手机调试授权及同一局域网')


async def _restore_usb(policy, serial, fingerprint):
    """只对重新核验的原设备发送一次 usb，再只读等待端口关闭；不把命令返回当恢复证明。"""
    target = next((item for item in await inventory(policy) if item['selector'] == serial
        and item['state'] == 'device' and item['transport_id']), None)
    if target is None or await hardware_identity(policy, target['transport_id'], canonical_serial=serial) != fingerprint:
        return False
    try:
        await _transport(policy, target['transport_id'], ['usb'])
    except HTTPException:
        # 连接随 adbd 重启中断也可能已经生效；只能读取状态核对，不能再发一次命令。
        pass
    deadline = time.monotonic() + ROLLBACK_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            current = next((item for item in await inventory(policy) if item['selector'] == serial
                and item['state'] == 'device' and item['transport_id']), None)
            if current and await hardware_identity(policy, current['transport_id'], canonical_serial=serial) == fingerprint:
                raw = await _transport(policy, current['transport_id'], ['shell', 'getprop', 'service.adb.tcp.port'])
                if raw.strip() in (b'', b'0', b'-1'):
                    return True
        except HTTPException:
            pass
        await asyncio.sleep(CONNECT_RETRY_INTERVAL)
    return False


async def enable_legacy(settings, serial, endpoint, *, allow_legacy_tcp=False, prefer_wireless=False):
    if not allow_legacy_tcp:
        raise HTTPException(409, 'Android 10 传统无线 ADB 不加密，只可在可信家庭局域网使用；明确接受后才能启用')
    endpoint = private_endpoint(endpoint)
    policy = load_policy(settings)
    _configured(policy, serial)
    usb = next((item for item in await inventory(policy) if item['selector'] == serial and item['state'] == 'device' and item['transport_id']), None)
    if usb is None or ':' in serial or serial.startswith('emulator-'):
        raise HTTPException(409, '首次启用传统无线 ADB 需要当前已授权的实体 USB 连接')
    fingerprint = await hardware_identity(policy, usb['transport_id'], canonical_serial=serial)
    port = int(endpoint.rsplit(':', 1)[1])
    current_port = (await _transport(policy, usb['transport_id'], ['shell', 'getprop', 'service.adb.tcp.port'])).strip()
    listener_changed = False
    try:
        if current_port != str(port).encode():
            # 派发前记住本轮可能已修改监听，即使 adbd 重启切断回执也会尝试安全恢复。
            listener_changed = True
            await _transport(policy, usb['transport_id'], ['tcpip', str(port)])
        target = await _explicit_connect(policy, endpoint)
        if await hardware_identity(policy, target['transport_id'], canonical_serial=serial) != fingerprint:
            raise HTTPException(403, 'USB 与无线连接的硬件身份不一致，已拒绝登记')
        binding = WirelessBinding(serial=serial, endpoint=endpoint, mode='legacy_tcp',
            identity_sha256=fingerprint, prefer_wireless=prefer_wireless)
        _save(settings, policy, binding)
    except BaseException as error:
        try:
            await _host(policy, ['disconnect', endpoint])
        except HTTPException:
            pass
        restored = False
        if listener_changed:
            try:
                restored = await _restore_usb(policy, serial, fingerprint)
            except HTTPException:
                pass
        if isinstance(error, Exception):
            outcome = ('已核实恢复 USB 调试模式，本次无线配置未保存' if restored else
                '未能确认恢复 USB，请在手机上关闭无线调试；本次无线配置未保存') if listener_changed else '原监听保持不变，本次无线配置未保存'
            message = str(error.detail) if isinstance(error, HTTPException) else '本机无线登记未完成'
            raise HTTPException(error.status_code if isinstance(error, HTTPException) else 503, message + '；' + outcome) from None
        raise
    return {'serial': serial, 'wireless_enabled': True, 'security': 'legacy_tcp', 'encrypted': False,
            'listener_restarted': listener_changed,
            'note': '传统无线 ADB 没有 TLS；仅限可信局域网，不可映射到公网'}


async def pair_wireless(settings, serial, pairing_endpoint, connection_endpoint, code, *, prefer_wireless=False):
    pairing_endpoint, endpoint = private_endpoint(pairing_endpoint), private_endpoint(connection_endpoint)
    if not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code):
        raise HTTPException(422, '无线配对码应为手机显示的六位数字')
    policy = load_policy(settings)
    old = _configured(policy, serial)
    paired = await _host(policy, ['pair', pairing_endpoint], timeout=30, stdin_data=(code + '\n').encode())
    if b'Successfully paired to ' not in paired:
        raise HTTPException(503, 'ADB 未确认配对成功，请核对配对码及配对端口')
    target = await _explicit_connect(policy, endpoint)
    try:
        sdk = int((await _transport(policy, target['transport_id'], ['shell', 'getprop', 'ro.build.version.sdk'])).strip())
        if sdk < 30:
            raise HTTPException(409, 'TLS 无线配对需要 Android 11 或更新版本')
        fingerprint = await hardware_identity(policy, target['transport_id'], canonical_serial=serial)
        await verify_tls_port(policy, target['transport_id'], endpoint)
        if old and old.identity_sha256 != fingerprint:
            raise HTTPException(403, '配对设备与原登记硬件身份不一致')
        binding = WirelessBinding(serial=serial, endpoint=endpoint, mode='tls',
            identity_sha256=fingerprint, prefer_wireless=prefer_wireless)
        _save(settings, policy, binding)
    except BaseException:
        await _host(policy, ['disconnect', endpoint])
        raise
    return {'serial': serial, 'wireless_enabled': True, 'security': 'tls', 'encrypted': True}


async def connect_registered(settings, serial, *, endpoint=None, prefer_wireless=None):
    policy = load_policy(settings)
    old = _configured(policy, serial)
    if old is None:
        raise HTTPException(409, '此设备尚未完成本机无线登记，请先使用 USB 启用或手机配对码配对')
    endpoint = private_endpoint(endpoint or old.endpoint)
    target = await _explicit_connect(policy, endpoint)
    try:
        if await hardware_identity(policy, target['transport_id'], canonical_serial=serial) != old.identity_sha256:
            raise HTTPException(403, '新的无线地址不是原登记设备')
        if old.mode == 'tls':
            await verify_tls_port(policy, target['transport_id'], endpoint)
        updated = old.model_copy(update={'endpoint': endpoint, 'enabled': True,
            'prefer_wireless': old.prefer_wireless if prefer_wireless is None else prefer_wireless})
        _save(settings, policy, updated)
    except BaseException:
        await _host(policy, ['disconnect', endpoint])
        raise
    return {'serial': serial, 'wireless_enabled': True, 'security': old.mode, 'encrypted': old.mode == 'tls'}


async def disable_wireless(settings, serial):
    policy = load_policy(settings)
    old = _configured(policy, serial)
    if old is None:
        return {'serial': serial, 'wireless_enabled': False, 'device_listener_disabled': False}
    remote_disabled = False
    if old.mode == 'legacy_tcp':
        try:
            target = await resolve_target(policy, serial)
            await _transport(policy, target['transport_id'], ['usb'])
            remote_disabled = True
        except HTTPException:
            pass
    # 始终先撤销产品内无线授权，保留成员绑定、稳定 serial 及输入法配置。
    _save(settings, policy, old.model_copy(update={'enabled': False, 'prefer_wireless': False}))
    try:
        await _host(policy, ['disconnect', old.endpoint])
    except HTTPException:
        pass
    return {'serial': serial, 'wireless_enabled': False, 'device_listener_disabled': remote_disabled,
            'note': '无线使用权已撤销，USB 可继续使用；请在手机中关闭无线调试' if not remote_disabled else '已要求设备恢复 USB 调试模式，无线使用权已撤销'}


def main():
    parser = argparse.ArgumentParser(description='仅本机配置成员设备的局域网无线 ADB；不扫描网络、不更改成员归属')
    parser.add_argument('action', choices=['legacy-enable', 'pair', 'connect', 'disable', 'status'])
    parser.add_argument('--state-dir', default='state')
    parser.add_argument('--serial')
    parser.add_argument('--endpoint', help='手机显示的局域网连接 IP:端口')
    parser.add_argument('--pair-endpoint', help='Android 11+ 手机配对窗口的 IP:配对端口，与连接端口独立')
    parser.add_argument('--allow-legacy-tcp', action='store_true', help='明确接受 Android 10 传统 ADB 非加密局域网连接')
    parser.add_argument('--prefer-wireless', action='store_true', default=None, help='无线在线时优先使用，方便在 USB 仍插着时验证')
    args = parser.parse_args()
    settings = SimpleNamespace(state_dir=Path(args.state_dir))
    async def run():
        if args.action == 'status':
            return await device_statuses(load_policy(settings))
        if not args.serial:
            parser.error('必须指定原登记的稳定设备 serial')
        if args.action in ('legacy-enable', 'pair') and not args.endpoint:
            parser.error('必须显式填写手机的局域网连接地址')
        if args.action == 'legacy-enable':
            return await enable_legacy(settings, args.serial, args.endpoint,
                allow_legacy_tcp=args.allow_legacy_tcp, prefer_wireless=bool(args.prefer_wireless))
        if args.action == 'pair':
            if not args.pair_endpoint:
                parser.error('必须显式填写独立的配对地址 --pair-endpoint')
            code = getpass.getpass('请输入手机显示的无线配对码（不会显示或保存）：')
            return await pair_wireless(settings, args.serial, args.pair_endpoint, args.endpoint,
                code, prefer_wireless=bool(args.prefer_wireless))
        if args.action == 'connect':
            return await connect_registered(settings, args.serial, endpoint=args.endpoint, prefer_wireless=args.prefer_wireless)
        return await disable_wireless(settings, args.serial)
    try:
        print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2))
    except HTTPException as error:
        parser.exit(1, str(error.detail) + '\n')


if __name__ == '__main__':
    main()
