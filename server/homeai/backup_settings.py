"""本机备份设置与调度；默认关闭，只有显式启用或手动请求才会执行。"""
import asyncio
import fcntl
import logging
import os
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, StrictBool, field_validator, model_validator

from .backup import development_database_name
from .contracts import Contract
from .security import Actor, authenticate, owner

router = APIRouter(prefix='/api/v1/manage', tags=['备份设置'])
log = logging.getLogger('homeai.backup-worker')
ROOT = Path(__file__).resolve().parents[2]
CONTEXT = 'server-backup-settings:v1'


class BackupSettingsInput(Contract):
    enabled: StrictBool = False
    directory: str = Field(default='', max_length=2000)
    frequency: Literal['manual', 'daily', 'weekly', 'interval'] = 'manual'
    interval_hours: int = Field(default=24, ge=1, le=8760, strict=True)

    @field_validator('directory')
    @classmethod
    def path_value(cls, value):
        value = value.strip()
        if value and (not Path(value).is_absolute() or '\x00' in value or '\n' in value or '\r' in value or '..' in Path(value).parts):
            raise ValueError('保存位置必须是服务器上的绝对目录路径')
        return str(Path(value)) if value else ''

    @model_validator(mode='after')
    def enabled_destination(self):
        if self.enabled and not self.directory:
            raise ValueError('启用备份前请填写服务器保存位置')
        return self


def period(config):
    if not config['enabled'] or config['frequency'] == 'manual':
        return None
    return {'daily': 86400, 'weekly': 604800, 'interval': config['interval_hours'] * 3600}[config['frequency']]


def validate_directory(value, create=False):
    if not value:
        raise ValueError('请先设置服务器保存位置')
    path = Path(value)
    # 防止配置或运行期间被符号链接转向意外位置。
    for item in [path, *path.parents]:
        if item.is_symlink():
            raise ValueError('备份保存位置及其父目录不能是符号链接')
        if item.exists() and not item.is_dir():
            raise ValueError('备份保存位置必须是目录')
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    existing = next((item for item in [path, *path.parents] if item.exists()), None)
    if existing is None or not os.access(existing, os.W_OK | os.X_OK):
        raise ValueError('备份保存位置不可写，请检查服务器目录权限')
    return path


def destination(app, value, create=False):
    path = validate_directory(value)
    source = app.settings.state_dir.resolve()
    resolved = path.resolve()
    if any(resolved.is_relative_to(source / name) for name in ('blobs', 'media', 'acme', 'tls', 'notifications')):
        raise ValueError('备份目录不能放在正在备份的数据或密钥源目录内部')
    return validate_directory(value, create=create)


class Store:
    def __init__(self, app):
        self.app = app
        self.directory = app.settings.state_dir
        self.path = self.directory / 'backup-settings.enc'
        self.key = self.directory / 'backup.key'

    @contextmanager
    def lock(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.directory / 'backup-settings.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def read(self):
        if self.path.is_symlink():
            raise ValueError('备份设置文件不能是符号链接')
        if not self.path.exists():
            return {'config': BackupSettingsInput().model_dump(), 'next_run_at': None, 'last_run': None}
        if not self.path.is_file() or self.path.stat().st_mode & 0o077 or self.path.stat().st_size > 50000:
            raise ValueError('备份设置文件权限或格式无效')
        value = self.app.vault.open(self.path.read_text(), CONTEXT)
        value['config'] = BackupSettingsInput.model_validate(value['config']).model_dump()
        return value

    def write(self, value):
        temporary = self.directory / ('backup-settings-' + uuid.uuid4().hex + '.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as output:
            output.write(self.app.vault.seal(value, CONTEXT))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.path)

    def key_ready(self):
        try:
            info = self.key.lstat()
            return stat.S_ISREG(info.st_mode) and not info.st_mode & 0o077 and info.st_size == 32
        except OSError:
            return False

    def ensure_key(self):
        if not self.key.exists() and not self.key.is_symlink():
            try:
                descriptor = os.open(self.key, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, 'wb') as output:
                    output.write(os.urandom(32))
                    output.flush()
                    os.fsync(output.fileno())
            except FileExistsError:
                pass
        if not self.key_ready():
            raise ValueError('备份密钥必须是独立的 32 字节私有普通文件（权限 0600），请在服务器检查；不会覆盖已有密钥')

    def save(self, body, actor_id):
        config = body.model_dump()
        if config['directory']:
            destination(self.app, config['directory'])
        with self.lock():
            value = self.read()
            old = value['config']
            interval = period(config)
            schedule_changed = any(old.get(field) != config[field] for field in ('enabled', 'directory', 'frequency', 'interval_hours'))
            if config['enabled']:
                self.ensure_key()
            value['config'] = config
            if not interval:
                value['next_run_at'] = None
            elif schedule_changed or value.get('next_run_at') is None:
                value['next_run_at'] = time.time() + interval
            value['updated_by'] = actor_id
            value['updated_at'] = time.time()
            self.write(value)
        return self.view(value)

    def request_run(self, actor_id):
        with self.lock():
            value = self.read()
            destination(self.app, value['config']['directory'])
            last = value.get('last_run')
            if last and last['status'] in {'queued', 'running'}:
                raise HTTPException(409, '已有备份正在排队或执行，请等待本次完成')
            self.ensure_key()
            value['last_run'] = self.new_run(value['config'], 'manual')
            value['last_run']['requested_by'] = actor_id
            self.write(value)
        return self.view(value)

    @staticmethod
    def new_run(config, reason):
        return {'id': uuid.uuid4().hex, 'status': 'queued', 'reason': reason, 'requested_at': time.time(),
                '_directory': config['directory']}

    def view(self, value=None):
        value = value or self.read()
        run = value.get('last_run')
        return {**value['config'], 'next_run_at': value.get('next_run_at'),
                'last_run': {key: run[key] for key in ('id', 'status', 'reason', 'requested_at', 'started_at', 'finished_at', 'file_name', 'bytes', 'error') if key in run} if run else None,
                'worker_online': worker_online(self.app), 'key_ready': self.key_ready(),
                'key_path': str(self.key), 'key_notice': '备份密钥保存在服务器，请另存到安全位置并与备份分开保管；丢失后无法恢复。页面不会显示密钥内容。'}


def suspend_restored_configuration(ciphertext, vault):
    value = vault.open(ciphertext, CONTEXT)
    value['config'] = BackupSettingsInput.model_validate(value['config']).model_dump()
    value['config']['enabled'] = False
    value['next_run_at'] = None
    run = value.get('last_run')
    if run and run.get('status') in {'queued', 'running'}:
        run['status'] = 'failed'
        run['finished_at'] = time.time()
        run['error'] = '恢复时已暂停旧备份请求，请核对配置后手动操作'
    return vault.seal(value, CONTEXT)


def configured_directory(app):
    value = Store(app).read()['config']['directory']
    return destination(app, value) if value else None


def worker_online(app):
    from .heartbeat import configuration_id
    for path in (app.settings.state_dir / 'health').glob('backup-worker-*.enc'):
        try:
            if path.is_symlink() or path.stat().st_size > 10000:
                continue
            value = app.vault.open(path.read_text(), 'worker-heartbeat:backup-worker')
            if value['configuration'] == configuration_id(app) and value['phase'] == 'running' and 0 <= time.time() - value['last_seen'] <= 30:
                return True
        except Exception:
            continue
    return False


def safe_operation(call):
    try:
        return call()
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except Exception:
        raise HTTPException(503, '备份设置读取或保存失败，请检查服务器私有配置与目录权限') from None


@router.get('/backup-settings')
def get_settings(request: Request, actor: Actor = Depends(authenticate)):
    owner(actor)
    return safe_operation(lambda: Store(request.app.state).view())


@router.put('/backup-settings')
def save_settings(body: BackupSettingsInput, request: Request, actor: Actor = Depends(authenticate)):
    owner(actor)
    return safe_operation(lambda: Store(request.app.state).save(body, actor.user_id))


@router.post('/backup-settings/run', status_code=202)
def request_backup(request: Request, actor: Actor = Depends(authenticate)):
    owner(actor)
    return safe_operation(lambda: Store(request.app.state).request_run(actor.user_id))


def run_command(arguments):
    # 不执行 Shell，也不把脚本输出或数据库连接信息写入普通日志与页面。
    subprocess.run(arguments, cwd=ROOT, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3 * 3600)


def cycle(app):
    store = Store(app)
    with store.lock():
        value = store.read()
        run = value.get('last_run')
        if run and run['status'] == 'running':
            # 单 worker 已持有进程锁；遗留 running 表示上一次进程异常退出，不能盲目重跑。
            run['status'] = 'failed'
            run['finished_at'] = time.time()
            run['error'] = '上次备份执行被中断，请核对保存目录后手动重试'
            interval = period(value['config'])
            value['next_run_at'] = time.time() + interval if interval else None
            store.write(value)
            return
        if not run or run['status'] != 'queued':
            interval = period(value['config'])
            due = value.get('next_run_at')
            if not interval or due is None or due > time.time():
                return
            run = store.new_run(value['config'], 'scheduled')
            value['last_run'] = run
        run['status'], run['started_at'] = 'running', time.time()
        store.write(value)
    failure = None
    output = None
    try:
        database_name = development_database_name(app.settings.database_url)
        output = destination(app, run['_directory'], create=True) / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + run['id'][:12] + '.haib')
        with store.lock():
            current = store.read()
            current['last_run']['file_name'] = output.name
            store.write(current)
        store.ensure_key()
        arguments = [sys.executable, str(ROOT / 'scripts/backup.py'), 'create', '--key', str(store.key.resolve()), '--file', str(output), '--state-dir', str(app.settings.state_dir.resolve()), '--database-name', database_name]
        run_command(arguments)
        run_command([*arguments[:2], 'verify', *arguments[3:]])
        if output.is_symlink() or not output.is_file() or output.stat().st_size == 0:
            raise ValueError('empty_output')
    except ValueError as exc:
        failure = str(exc) if str(exc).startswith(('当前备份仅支持', '数据库名称无效', '数据库地址无效')) else '备份未完成，请检查服务器密钥和保存目录'
    except subprocess.TimeoutExpired:
        failure = '备份超过执行时限，文件可能未完整，请核对后手动重试'
    except subprocess.CalledProcessError:
        failure = '备份或完整性校验失败，请检查数据库、Docker 和目录权限'
    except Exception:
        failure = '备份未完成，请检查服务器密钥和保存目录'
    with store.lock():
        current = store.read()
        if current.get('last_run', {}).get('id') != run['id']:
            return
        current['last_run']['status'] = 'failed' if failure else 'succeeded'
        current['last_run']['finished_at'] = time.time()
        if failure:
            current['last_run']['error'] = failure
        else:
            current['last_run']['bytes'] = output.stat().st_size
        interval = period(current['config'])
        current['next_run_at'] = time.time() + interval if interval else None
        store.write(current)


async def main():
    from .api import create_app
    from .heartbeat import Heartbeat
    app = create_app().state
    path = app.settings.state_dir / 'backup-worker.lock'
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'r+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('已有备份 Worker 运行') from None
        async with Heartbeat(app, 'backup-worker') as heartbeat:
            while True:
                try:
                    await asyncio.to_thread(cycle, app)
                    heartbeat.progress()
                except Exception as exc:
                    heartbeat.progress(exc)
                    log.error('备份调度检查失败：%s', type(exc).__name__)
                await asyncio.sleep(5)
