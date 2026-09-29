"""APNs 管理配置。密钥仅在家庭服务器解密，配置文件缺失时兼容环境配置。"""
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, field_validator

from .contracts import Contract
from .crypto import canonical, digest
from .data import audit
from .db import now, scope
from .private_files import private_write
from .security import Actor, authenticate, owner

router = APIRouter(prefix='/api/v1/manage', tags=['通知配置'])
AAD = 'notification-configuration:apns:v1'


@dataclass(frozen=True)
class APNsConfiguration:
    enabled: bool = False
    key_id: str = ''
    team_id: str = ''
    topic: str = ''
    private_key: str = field(default='', repr=False)
    source: str = 'none'
    error: bool = False

    @property
    def revision(self):
        return digest(canonical({'enabled': self.enabled, 'key_id': self.key_id, 'team_id': self.team_id,
                                 'topic': self.topic, 'private_key': self.private_key, 'error': self.error}))

    @property
    def status(self):
        if self.error:
            return 'invalid_configuration'
        if not self.enabled:
            return 'disabled' if self.source == 'managed' else 'not_configured'
        if not self.key_id or not self.team_id or not self.topic or not self.private_key:
            return 'invalid_configuration'
        if not valid_identifiers(self.key_id, self.team_id, self.topic):
            return 'invalid_configuration'
        try:
            parse_key(self.private_key)
        except ValueError:
            return 'invalid_configuration'
        return 'ready'


def valid_identifiers(key_id, team_id, topic):
    return bool(re.fullmatch('[A-Z0-9]{10}', key_id) and re.fullmatch('[A-Z0-9]{10}', team_id)
                and re.fullmatch('[A-Za-z0-9.-]{3,255}', topic))


def parse_key(value):
    try:
        if not isinstance(value, str) or len(value) > 10000:
            raise ValueError()
        key = serialization.load_pem_private_key(value.encode(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError()
        return key
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise ValueError('需要未加密的 P-256 APNs 私钥 PEM（.p8）') from None


def private_read(path, maximum):
    """拒绝链接、其他用户可读文件及超限文件，不在报错中带出密钥路径或内容。"""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'r') as file:
        metadata = os.fstat(file.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > maximum:
            raise ValueError('配置文件权限或大小不符合要求')
        value = file.read(maximum + 1)
        if len(value) > maximum:
            raise ValueError('配置文件超过大小限制')
        return value


def configuration_path(app):
    return app.settings.state_dir / 'notifications' / 'apns.enc'


def environment_configuration(settings):
    path, key_id, team_id, topic = [getattr(settings, key, None) for key in ('apns_key_file', 'apns_key_id', 'apns_team_id', 'apns_topic')]
    if not any((path, key_id, team_id, topic)):
        return APNsConfiguration()
    try:
        private_key = private_read(Path(path), 10000) if path else ''
        return APNsConfiguration(True, key_id or '', team_id or '', topic or '', private_key, 'environment')
    except (OSError, ValueError, TypeError, UnicodeError):
        return APNsConfiguration(True, key_id or '', team_id or '', topic or '', source='environment', error=True)


def resolve_configuration(app):
    path = configuration_path(app)
    if not path.exists() and not path.is_symlink():
        return environment_configuration(app.settings)
    try:
        content = app.vault.open(private_read(path, 32000), AAD)
        if content.get('version') != 1 or type(content.get('enabled')) is not bool:
            raise ValueError()
        for key in ('key_id', 'team_id', 'topic', 'private_key'):
            if not isinstance(content.get(key), str):
                raise ValueError()
        return APNsConfiguration(content['enabled'], content['key_id'], content['team_id'],
                                 content['topic'], content['private_key'], 'managed')
    except Exception:
        # 已保存配置损坏时拒绝推送，不能自动回退到旧环境凭据。
        return APNsConfiguration(source='managed', error=True)


class NotificationConfigurationInput(Contract):
    enabled: bool
    key_id: str = Field(default='', max_length=10)
    team_id: str = Field(default='', max_length=10)
    topic: str = Field(default='', max_length=255)
    private_key: str | None = Field(default=None, max_length=10000, repr=False)

    @field_validator('key_id', 'team_id', 'topic', 'private_key', mode='before')
    @classmethod
    def trim(cls, value):
        return value.strip() if isinstance(value, str) else value


def configuration_view(app):
    from .notifications import worker_online
    config = resolve_configuration(app)
    online = worker_online(app)
    status = config.status
    return {'enabled': config.enabled, 'key_id': config.key_id, 'team_id': config.team_id, 'topic': config.topic,
            'private_key_configured': bool(config.private_key), 'push_configured': status == 'ready',
            'worker_online': online, 'status': status if status != 'ready' or online else 'worker_offline',
            'source': config.source, 'validation': 'structure_only'}


@router.get('/notifications')
def get_configuration(request: Request, actor: Actor = Depends(authenticate)):
    owner(actor)
    return configuration_view(request.app.state)


@router.put('/notifications')
def save_configuration(body: NotificationConfigurationInput, request: Request, actor: Actor = Depends(authenticate)):
    owner(actor)
    app = request.app.state
    previous = resolve_configuration(app)
    # 空白仅保留，不清除；暂停不会重新启用 .env 中的配置。
    private_key = body.private_key or previous.private_key
    candidate = APNsConfiguration(body.enabled, body.key_id, body.team_id, body.topic, private_key, 'managed')
    if body.private_key:
        try:
            parse_key(body.private_key)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
    if body.enabled and candidate.status != 'ready':
        raise HTTPException(422, '启用通知需要有效的 Key ID、Team ID、Bundle ID 和 P-256 私钥；尚未验证 Apple 权限或实际投递')
    if any(value and not re.fullmatch('[A-Z0-9]{10}', value) for value in (body.key_id, body.team_id)) or (body.topic and not re.fullmatch('[A-Za-z0-9.-]{3,255}', body.topic)):
        raise HTTPException(422, 'Key ID、Team ID 需要 10 位大写字母或数字；Bundle ID 格式无效')
    document = {'version': 1, 'enabled': candidate.enabled, 'key_id': candidate.key_id, 'team_id': candidate.team_id,
                'topic': candidate.topic, 'private_key': candidate.private_key, 'updated_at': now()}
    path = configuration_path(app)
    if path.is_symlink() or path.parent.is_symlink():
        raise HTTPException(409, '通知配置路径不能是符号链接')
    private_write(path, app.vault.seal(document, AAD))
    with app.db() as db:
        scope(db, actor.user_id, actor.household_id)
        audit(db, actor, 'notifications.configure', 'apns', {'enabled': body.enabled, 'key_replaced': bool(body.private_key)})
        db.commit()
    return configuration_view(app)
