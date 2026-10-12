"""本机固定 ADB 执行器；手机与模型只能提交已定义的动作，不能提交 Shell。"""
import asyncio
import base64
import hashlib
import ipaddress
import xml.etree.ElementTree as ET
import json
import os
import re
import stat
import struct
from pathlib import Path
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SERIAL = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
PACKAGE = re.compile(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+\Z')
MAX_SCREEN_BYTES = 16 * 1024 * 1024



def private_endpoint(value):
    """无线 ADB 只允许显式的局域网字面地址，不解析域名、不接受公网或环回目标。"""
    if not isinstance(value, str):
        raise ValueError('无线地址必须是 IP:端口')
    try:
        if value.startswith('['):
            host, port = value[1:].split(']:', 1)
        else:
            host, port = value.rsplit(':', 1)
        address = ipaddress.ip_address(host)
        number = int(port)
        networks = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16', 'fc00::/7')
        if '%' in host or not 1 <= number <= 65535 or not any(address in ipaddress.ip_network(item) for item in networks):
            raise ValueError()
        return f'[{address}]:{number}' if address.version == 6 else f'{address}:{number}'
    except (ValueError, TypeError):
        raise ValueError('无线 ADB 必须使用局域网 IP 和明确端口，不能使用公网、域名或环回地址') from None


class WirelessBinding(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    serial: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,128}$')
    endpoint: str
    mode: str = Field(pattern=r'^(tls|legacy_tcp)$')
    identity_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    enabled: bool = True
    prefer_wireless: bool = False

    @field_validator('endpoint')
    @classmethod
    def local_address(cls, value):
        return private_endpoint(value)

class AndroidPolicy(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    enabled: bool = False
    adb_binary: str = ''
    serials: list[str] = Field(default_factory=list, max_length=100)
    ime_serials: list[str] = Field(default_factory=list, max_length=100)
    wireless: list[WirelessBinding] = Field(default_factory=list, max_length=100)

    @field_validator('serials', 'ime_serials')
    @classmethod
    def serial_list(cls, values):
        if any(not SERIAL.fullmatch(value) for value in values):
            raise ValueError('Android 设备序号不合法')
        return list(dict.fromkeys(values))

    @model_validator(mode='after')
    def wireless_scope(self):
        if any(item.serial not in self.serials for item in self.wireless):
            raise ValueError('无线设备必须已登记稳定的本机设备标识')
        if len({item.serial for item in self.wireless}) != len(self.wireless):
            raise ValueError('同一设备不能重复登记无线配置')
        if len({item.endpoint for item in self.wireless if item.enabled}) != sum(item.enabled for item in self.wireless):
            raise ValueError('同一无线地址不能分配给多个设备')
        return self


def policy_path(settings):
    return Path(getattr(settings, 'android_policy_file', None) or Path(settings.state_dir) / 'android-policy.json')


def load_policy(settings):
    path = policy_path(settings)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return AndroidPolicy()
    except OSError:
        raise HTTPException(503, '无法安全读取本机 Android 执行配置') from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise HTTPException(503, 'Android 执行配置必须是服务账户拥有的 0600 普通文件')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise HTTPException(503, 'Android 执行配置超过大小限制')
        try:
            policy = AndroidPolicy.model_validate_json(raw)
        except ValueError:
            raise HTTPException(503, 'Android 执行配置不符合契约') from None
        if policy.enabled:
            binary = Path(policy.adb_binary)
            if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
                raise HTTPException(503, '需要登记可执行的绝对 ADB 路径')
            if binary.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise HTTPException(503, 'ADB 文件不能允许组或其他账户写入')
        return policy
    finally:
        os.close(fd)


async def command_prefix(policy, serial):
    """返回绑定到当前已核验 transport 的命令前缀；IP 变化不会改变成员设备身份。"""
    if not policy.enabled:
        raise HTTPException(409, 'Android 执行器尚未由本机启用')
    if serial is None:
        return [policy.adb_binary]
    if not SERIAL.fullmatch(serial) or serial not in policy.serials:
        raise HTTPException(403, 'Android 设备未获得本机执行授权')
    if any(item.serial == serial for item in getattr(policy, 'wireless', [])):
        from .android_wireless import resolve_target
        target = await resolve_target(policy, serial)
        return [policy.adb_binary, '-t', str(target['transport_id'])]
    return [policy.adb_binary, '-s', serial]


async def _run(policy, serial, args, *, limit=65536, timeout=8):
    argv = await command_prefix(policy, serial)
    return await _execute_argv(argv + list(args), limit=limit, timeout=timeout)


async def _execute_argv(argv, *, limit=65536, timeout=8, stdin_data=None):
    """本机代码使用的固定 argv 执行边界；配对码仅经标准输入，不能进入 argv。"""
    environment = {key: os.environ[key] for key in ('HOME', 'USER', 'TMPDIR') if key in os.environ}
    environment['PATH'] = '/usr/bin:/bin'
    process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
        env=environment)
    async def bounded(stream, maximum):
        chunks, length = [], 0
        while chunk := await stream.read(65536):
            length += len(chunk)
            if length > maximum:
                raise HTTPException(502, 'Android 返回内容超过限制')
            chunks.append(chunk)
        return b''.join(chunks)
    try:
        if stdin_data is not None:
            process.stdin.write(stdin_data)
            await process.stdin.drain()
            process.stdin.close()
        output, errors = await asyncio.wait_for(asyncio.gather(
            bounded(process.stdout, limit), bounded(process.stderr, 32768)), timeout)
        await asyncio.wait_for(process.wait(), 1)
        if process.returncode:
            # ADB 原始输出可能包含屏幕文字，不能进入一般错误日志。
            raise HTTPException(502, 'Android 命令未完成，请检查设备连接及系统授权')
        return output
    except asyncio.TimeoutError:
        raise HTTPException(504, 'Android 操作超时，需核对设备当前状态') from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def devices(settings):
    policy = load_policy(settings)
    if not policy.enabled:
        return []
    from .android_wireless import device_statuses
    return await device_statuses(policy)


def png_size(raw):
    if len(raw) < 33 or raw[:8] != b'\x89PNG\r\n\x1a\n' or raw[12:16] != b'IHDR':
        raise HTTPException(502, 'Android 截图格式无效')
    width, height = struct.unpack('>II', raw[16:24])
    if not (1 <= width <= 8192 and 1 <= height <= 8192 and width * height <= 32_000_000):
        raise HTTPException(502, 'Android 截图尺寸超出限制')
    return width, height


async def screenshot(settings, serial):
    raw = await _run(load_policy(settings), serial, ['exec-out', 'screencap', '-p'], limit=MAX_SCREEN_BYTES)
    width, height = png_size(raw)
    return raw, width, height


async def read_sdk(settings, serial):
    raw = await _run(load_policy(settings), serial, ['shell', 'getprop', 'ro.build.version.sdk'], limit=128)
    try:
        value = int(raw.strip())
        if not 21 <= value <= 100:
            raise ValueError()
        return value
    except ValueError:
        raise HTTPException(502, '无法确定安卓系统版本') from None


async def display_size(settings, serial):
    raw = await _run(load_policy(settings), serial, ['shell', 'wm', 'size'], limit=4096)
    sizes = re.findall(rb'(?:Physical|Override) size:\s*(\d+)x(\d+)', raw)
    if not sizes:
        raise HTTPException(502, '无法核对安卓显示尺寸')
    width, height = (int(value) for value in sizes[-1])
    if not 1 <= width <= 8192 or not 1 <= height <= 8192:
        raise HTTPException(502, '安卓显示尺寸超过限制')
    return width, height


async def execute(settings, serial, action, *, width=0, height=0):
    name = action.action
    if name in ('tap', 'swipe'):
        x, y = round(action.x * (width - 1)), round(action.y * (height - 1))
        args = ['shell', 'input', name, str(x), str(y)]
        if name == 'swipe':
            args += [str(round(action.x2 * (width - 1))), str(round(action.y2 * (height - 1))), '300']
    elif name in ('back', 'home'):
        args = ['shell', 'input', 'keyevent', '4' if name == 'back' else '3']
    elif name == 'text':
        policy = load_policy(settings)
        if serial in policy.ime_serials:
            current = (await _run(policy, serial, ['shell', 'settings', 'get', 'secure', 'default_input_method'])).decode().strip()
            if current != 'dev.homeai.androidinput/.HomeAIInputService':
                raise HTTPException(409, '个人设备尚未启用 Home AI 输入法，请在设备上完成配置')
            encoded = base64.b64encode(action.text.encode()).decode()
            result = await _run(policy, serial, ['shell', 'am', 'broadcast', '-a', 'dev.homeai.input.COMMIT',
                '-p', 'dev.homeai.androidinput', '--es', 'text_base64', encoded])
            if not re.search(rb'Broadcast completed: result=1(?:,|\s|$)', result):
                raise HTTPException(409, '当前页面没有可输入的编辑框，文字未提交')
            return result
        # adb shell 会再次解释参数；严格白名单防止命令拼接与 input 的 % 转义。
        if not re.fullmatch(r'[A-Za-z0-9 .,@_+\-]{1,500}', action.text or ''):
            raise HTTPException(422, '当前 Android 输入支持英文、数字和常用符号；中文请在设备上输入')
        args = ['shell', 'input', 'text', action.text.replace(' ', '%s')]
    elif name == 'launch':
        if not PACKAGE.fullmatch(action.package or ''):
            raise HTTPException(422, 'Android 应用标识无效')
        args = ['shell', 'monkey', '-p', action.package, '-c', 'android.intent.category.LAUNCHER', '1']
    else:
        raise HTTPException(422, '不支持的 Android 操作')
    return await _run(load_policy(settings), serial, args)



async def hierarchy(settings, serial):
    """只读取当前 UI 树；临时文件固定在设备调试目录，不保留屏幕文本。"""
    policy = load_policy(settings)
    path = '/data/local/tmp/homeai-ui.xml'
    try:
        await _run(policy, serial, ['shell', 'uiautomator', 'dump', '--compressed', path], timeout=12)
        raw = await _run(policy, serial, ['exec-out', 'cat', path], limit=512 * 1024)
        if b'<hierarchy' not in raw or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
            raise HTTPException(502, '当前 Android 页面无法提供安全的控件树')
        return raw
    finally:
        try:
            await asyncio.shield(_run(policy, serial, ['shell', 'rm', '-f', path], timeout=3))
        except (HTTPException, OSError):
            pass


async def focus_signature(settings, serial, *, xml=None):
    """焦点标识只包含窗口与控件身份，不读取或保存编辑框原文。"""
    raw = await hierarchy(settings, serial) if xml is None else xml
    if len(raw) > 512 * 1024 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise HTTPException(502, 'Android 焦点控件树不合法')
    try:
        tree = ET.fromstring(raw)
    except ET.ParseError:
        raise HTTPException(502, '无法识别 Android 输入焦点') from None
    focused = [node for node in tree.iter('node') if node.get('focused') == 'true'
        and node.get('enabled') != 'false' and
        (node.get('class', '').endswith('EditText') or node.get('editable') == 'true')]
    if len(focused) != 1:
        raise HTTPException(409, '当前没有唯一的可编辑输入框，请先点击需要填写的位置')
    node = focused[0]
    package, kind, bounds = node.get('package', ''), node.get('class', ''), node.get('bounds', '')
    if not PACKAGE.fullmatch(package) or not re.fullmatch(r'\[\d+,\d+\]\[\d+,\d+\]', bounds):
        raise HTTPException(409, '当前输入框身份无法可靠确认，请在 Android 设备上输入')
    windows = await _run(load_policy(settings), serial, ['shell', 'dumpsys', 'window'],
        limit=2 * 1024 * 1024)
    # 使用窗口句柄避免同一 App 中相同坐标的不同页面误接收文字；不保留窗口标题。
    match = re.search(rb'mCurrentFocus=Window\{([A-Za-z0-9]+)\s', windows)
    if match is None:
        raise HTTPException(409, '无法确认前台输入窗口，请在 Android 设备上输入')
    identity = [package, node.get('resource-id', ''), kind, bounds,
                tree.get('rotation', ''), match.group(1).decode('ascii')]
    return {'fingerprint': hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest(),
            'password': node.get('password', '').lower() == 'true'}

def main():
    """仅本机终端登记设备；网页不能自行添加 ADB 路径或主机。"""
    import argparse
    import tempfile
    from types import SimpleNamespace
    parser = argparse.ArgumentParser(description='登记家庭 Android 执行设备（不安装应用、不改变账号）')
    parser.add_argument('--state-dir', default='state', help='家庭服务器状态目录')
    parser.add_argument('--adb', required=True, help='ADB 可执行文件的绝对路径')
    parser.add_argument('--serial', action='append', default=[], help='允许绑定的设备序号，可重复')
    parser.add_argument('--ime-serial', action='append', default=[], help='已完成 Home AI 输入法配置的设备序号')
    parser.add_argument('--disabled', action='store_true', help='登记配置但关闭执行')
    args = parser.parse_args()
    from .android_wireless import update_policy
    settings = SimpleNamespace(state_dir=Path(args.state_dir))
    def merge(current):
        values = current.model_dump(mode='json')
        values.update(enabled=not args.disabled, adb_binary=str(Path(args.adb).resolve()),
            serials=list(dict.fromkeys(current.serials + args.serial)),
            ime_serials=list(dict.fromkeys(current.ime_serials + args.ime_serial)))
        result = AndroidPolicy.model_validate(values)
        if not set(result.ime_serials).issubset(result.serials):
            parser.error('输入法设备必须同时登记在设备白名单')
        if result.enabled and not result.serials:
            parser.error('启用前至少登记一台设备')
        return result
    policy = update_policy(settings, merge)
    print(json.dumps({'enabled': policy.enabled, 'registered_devices': len(policy.serials),
                      'configured_input_methods': len(policy.ime_serials)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
