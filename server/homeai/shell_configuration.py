"""仅由家庭服务器本机管理员维护的 Shell 策略；网页和模型不能写入执行权限。"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from contextlib import contextmanager

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MAX_POLICY_BYTES = 1024 * 1024
IMMUTABLE_IMAGE = re.compile(r'(?:sha256:[0-9a-f]{64}|[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[0-9a-f]{64})\Z')


class ShellConfigurationError(HTTPException):
    def __init__(self, detail):
        super().__init__(503, detail)


class HostAction(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=2000)
    impact_summary: str = Field(default='', max_length=2000)
    file_changes: list[str] = Field(default_factory=list, max_length=32)
    service_changes: list[str] = Field(default_factory=list, max_length=32)
    script: str = Field(min_length=1, max_length=65536)
    cwd: str
    timeout_seconds: int = Field(default=60, ge=1, le=300)

    @field_validator('script', 'title', 'description', 'cwd', 'impact_summary')
    @classmethod
    def no_null(cls, value):
        if '\0' in value:
            raise ValueError('字段不能包含空字符')
        return value

    @field_validator('file_changes', 'service_changes')
    @classmethod
    def bounded_changes(cls, values):
        if any(not item.strip() or len(item) > 500 or '\0' in item for item in values):
            raise ValueError('影响说明不能为空、包含空字符或超过 500 字')
        return values

    @field_validator('script', 'title')
    @classmethod
    def meaningful(cls, value):
        if not value.strip():
            raise ValueError('内容不能为空')
        return value

    @field_validator('cwd')
    @classmethod
    def absolute_cwd(cls, value):
        if not Path(value).is_absolute():
            raise ValueError('工作目录必须是绝对路径')
        return value


class ShellPolicy(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: int = Field(default=1, ge=1, le=1)
    sandbox_enabled: bool = False
    sandbox_image: str = ''
    sandbox_runtime: str | None = None
    sandbox_members: list[str] = Field(default_factory=list, max_length=1000)
    host_enabled: bool = False
    host_members: list[str] = Field(default_factory=list, max_length=1000)
    host_actions: list[HostAction] = Field(default_factory=list, max_length=200)

    @field_validator('sandbox_image')
    @classmethod
    def immutable_image(cls, value):
        if value and not IMMUTABLE_IMAGE.fullmatch(value):
            raise ValueError('沙箱镜像必须固定为 sha256 镜像 ID 或仓库摘要')
        return value

    @field_validator('sandbox_runtime')
    @classmethod
    def runtime_name(cls, value):
        if value is not None and not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}', value):
            raise ValueError('容器运行时名称无效')
        return value

    @field_validator('sandbox_members', 'host_members')
    @classmethod
    def member_ids(cls, values):
        if any(not re.fullmatch(r'[a-zA-Z0-9_.-]{1,128}', item) for item in values):
            raise ValueError('成员标识无效')
        return list(dict.fromkeys(values))

    @model_validator(mode='after')
    def valid_policy(self):
        if self.sandbox_enabled and not self.sandbox_image:
            raise ValueError('启用沙箱前必须固定镜像')
        if len({action.id for action in self.host_actions}) != len(self.host_actions):
            raise ValueError('宿主动作标识不能重复')
        return self


def fingerprint(value):
    if isinstance(value, BaseModel):
        value = value.model_dump(mode='json')
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def policy_path(settings):
    return Path(getattr(settings, 'shell_policy_file', None) or Path(settings.state_dir) / 'shell-policy.json')


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('策略包含重复字段')
        value[key] = item
    return value


def load_policy(settings):
    path = policy_path(settings)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return ShellPolicy()
    except OSError:
        raise ShellConfigurationError('无法安全读取本机 Shell 策略，请检查文件类型和权限') from None
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ShellConfigurationError('本机 Shell 策略必须由服务账户拥有，且为权限 0600 的普通文件')
        with os.fdopen(fd, 'rb', closefd=False) as source:
            raw = source.read(MAX_POLICY_BYTES + 1)
        if len(raw) > MAX_POLICY_BYTES:
            raise ShellConfigurationError('本机 Shell 策略超过大小限制')
        try:
            value = json.loads(raw, object_pairs_hook=_unique_object)
            return ShellPolicy.model_validate(value)
        except (ValueError, UnicodeError, TypeError):
            raise ShellConfigurationError('本机 Shell 策略不符合契约，请核对版本、动作及固定镜像摘要') from None
    finally:
        os.close(fd)


def host_action(settings, actor, action_id):
    if actor.role != 'infrastructure_owner':
        raise HTTPException(403, '宿主管理仅供家庭基础设施管理员使用')
    policy = load_policy(settings)
    if actor.user_id not in policy.host_members:
        raise HTTPException(403, '当前管理员尚未获得本机宿主管理授权')
    if not policy.host_enabled:
        raise HTTPException(409, '宿主管理尚未由本机管理员启用')
    action = next((item for item in policy.host_actions if item.id == action_id), None)
    if action is None:
        raise HTTPException(404, '宿主管理动作未登记或已撤销')
    if not Path(action.cwd).is_dir():
        raise HTTPException(409, '宿主管理动作的固定工作目录不可用')
    action_digest = fingerprint(action)
    return {**action.model_dump(mode='json'), 'fingerprint': action_digest,
            'action_fingerprint': action_digest, 'policy_fingerprint': fingerprint(policy)}


def status(settings, actor, include_scripts=False):
    policy = load_policy(settings)
    administrator = actor.role == 'infrastructure_owner'
    host_allowed = administrator and actor.user_id in policy.host_members
    allowed = actor.user_id in policy.sandbox_members
    environment = getattr(settings, 'environment', 'development')
    sandbox_ready = bool(policy.sandbox_enabled and policy.sandbox_image and allowed)
    reason = ''
    if not policy.sandbox_enabled:
        reason = '隔离 Shell 尚未由本机管理员启用'
    elif not allowed:
        reason = '当前成员尚未获得隔离 Shell 使用权限'
    elif environment == 'production' and policy.sandbox_runtime != 'runsc':
        sandbox_ready = False
        reason = '生产隔离 Shell 需要通过验证的 runsc 运行时'
    actions = []
    if host_allowed:
        for action in policy.host_actions:
            item = action.model_dump(mode='json')
            item['fingerprint'] = fingerprint(action)
            if not include_scripts:
                item.pop('script')
                item.pop('cwd')
            actions.append(item)
    return {'sandbox': {'enabled': policy.sandbox_enabled, 'ready': sandbox_ready,
                        'image': policy.sandbox_image if administrator else '',
                        'runtime': policy.sandbox_runtime, 'members': policy.sandbox_members if administrator else [],
                        'reason': reason},
            'host': {'enabled': bool(policy.host_enabled and host_allowed), 'actions': actions,
                     'reason': '' if policy.host_enabled and host_allowed else '宿主管理需由本机启用，并仅供获得本机授权的基础设施管理员审批执行'},
            'policy_file': str(policy_path(settings)) if administrator else '', 'environment': environment}


def _write_policy(path, policy, *, create=False):
    """本机 CLI 使用原子替换，配置写入途中不会出现半份可执行策略。"""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ShellConfigurationError('不能向符号链接写入 Shell 策略')
    raw = (json.dumps(policy.model_dump(mode='json'), ensure_ascii=False, indent=2) + '\n').encode()
    if len(raw) > MAX_POLICY_BYTES:
        raise ShellConfigurationError('本机 Shell 策略超过大小限制，现有配置未替换')
    if create:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'wb', closefd=False) as target:
                target.write(raw)
                target.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
        return
    fd, temporary = tempfile.mkstemp(prefix='.shell-policy-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb', closefd=False) as target:
            target.write(raw)
            target.flush()
            os.fsync(fd)
        os.replace(temporary, path)
    finally:
        os.close(fd)
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _policy_lock(path):
    """序列化本机 CLI 的读取和替换，避免并发登记动作互相覆盖。"""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path.parent / ('.' + path.name + '.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ShellConfigurationError('本机 Shell 策略锁文件的类型或权限不安全')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description='仅在家庭服务器本机登记 Shell 权限和固定宿主管理动作')
    parser.add_argument('--policy-file', type=Path, default=Path('state/shell-policy.json'))
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='创建权限 0600 的关闭状态配置，不覆盖已有策略')
    sandbox = commands.add_parser('enable-sandbox', help='启用固定镜像的隔离 Shell 并指定成员')
    sandbox.add_argument('--image', required=True)
    sandbox.add_argument('--runtime')
    sandbox.add_argument('--member', action='append', required=True)
    commands.add_parser('disable-sandbox')
    host = commands.add_parser('enable-host', help='明确允许指定管理员在逐次审批后执行已登记的宿主动作')
    host.add_argument('--member', action='append', required=True)
    commands.add_parser('disable-host')
    action = commands.add_parser('add-host-action', help='登记或更新完整固定脚本；不会自动启用宿主管理')
    action.add_argument('--id', required=True)
    action.add_argument('--title', required=True)
    action.add_argument('--description', default='')
    action.add_argument('--impact-summary', default='', help='展示给审批人的预计影响；不会据此减少审批要求')
    action.add_argument('--file-change', action='append', default=[], help='文件新增、修改或删除范围，可重复指定')
    action.add_argument('--service-change', action='append', default=[], help='服务、软件或系统配置变动，可重复指定')
    action.add_argument('--script-file', type=Path, required=True)
    action.add_argument('--cwd', required=True)
    action.add_argument('--timeout', type=int, default=60)
    remove = commands.add_parser('remove-host-action')
    remove.add_argument('--id', required=True)
    args = parser.parse_args(argv)
    from types import SimpleNamespace
    settings = SimpleNamespace(shell_policy_file=args.policy_file, state_dir=args.policy_file.parent)
    try:
        with _policy_lock(args.policy_file):
            if args.command == 'init':
                _write_policy(args.policy_file, ShellPolicy(), create=True)
            else:
                policy = load_policy(settings)
                data = policy.model_dump(mode='json')
                if args.command == 'enable-sandbox':
                    data.update(sandbox_enabled=True, sandbox_image=args.image, sandbox_runtime=args.runtime, sandbox_members=args.member)
                elif args.command == 'disable-sandbox':
                    data['sandbox_enabled'] = False
                elif args.command in {'enable-host', 'disable-host'}:
                    data['host_enabled'] = args.command == 'enable-host'
                    if args.command == 'enable-host':
                        data['host_members'] = args.member
                elif args.command == 'add-host-action':
                    with args.script_file.open() as source:
                        script = source.read(65537)
                    item = HostAction(id=args.id, title=args.title, description=args.description, script=script,
                                      cwd=args.cwd, timeout_seconds=args.timeout, impact_summary=args.impact_summary,
                                      file_changes=args.file_change, service_changes=args.service_change)
                    data['host_actions'] = [existing for existing in data['host_actions'] if existing['id'] != args.id] + [item.model_dump(mode='json')]
                elif args.command == 'remove-host-action':
                    data['host_actions'] = [existing for existing in data['host_actions'] if existing['id'] != args.id]
                _write_policy(args.policy_file, ShellPolicy.model_validate(data))
    except HTTPException as exc:
        parser.exit(1, str(exc.detail) + '\n')
    except (OSError, ValueError, TypeError):
        parser.exit(1, '操作失败，请检查本机文件权限、固定镜像摘要和动作字段；现有策略未替换。\n')
    print('Shell 策略已保存；原审批在策略变化后失效，新执行仍需通过服务端审批。')


if __name__ == '__main__':
    main()
