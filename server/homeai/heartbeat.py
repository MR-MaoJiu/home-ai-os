"""认证心跳只表示事件循环近期响应，不替代任务和 Provider 业务验收。"""
import asyncio
import hashlib
import json
import os
import uuid
from pathlib import Path
from .db import now


def configuration_id(app):
    settings = app.settings
    values = {name: getattr(settings, name) for name in ('database_url', 'opa_url', 'nats_url', 'event_stream', 'event_subject_prefix', 'environment')}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


class Heartbeat:
    def __init__(self, app, service):
        self.app, self.service = app, service
        self.identifier = uuid.uuid4().hex
        self.started_at = now()
        self.configuration = configuration_id(app)
        self.last_cycle_at = None
        self.cycles = 0
        self.error_type = None
        self.phase = 'starting'
        self.path = app.settings.state_dir / 'health' / (service + '-' + self.identifier + '.enc')

    def progress(self, error=None):
        self.last_cycle_at = now()
        self.cycles += 1
        self.error_type = type(error).__name__ if error else None
        self.phase = 'error' if error else 'running'

    def write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = {'service': self.service, 'instance': self.identifier, 'pid': os.getpid(),
            'configuration': self.configuration, 'started_at': self.started_at, 'last_seen': now(), 'last_cycle_at': self.last_cycle_at,
            'cycles': self.cycles, 'phase': self.phase, 'error_type': self.error_type}
        temporary = self.path.with_suffix('.tmp-' + uuid.uuid4().hex)
        encrypted = self.app.vault.seal(data, 'worker-heartbeat:' + self.service)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as file: file.write(encrypted)
        os.replace(temporary, self.path)

    async def __aenter__(self):
        self.write()
        self.task = asyncio.create_task(self.pulse())
        return self

    async def pulse(self):
        while True:
            await asyncio.sleep(5)
            self.write()

    async def __aexit__(self, *_):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        self.phase = 'stopped'
        self.write()


def status(app, max_age=30):
    result = {}
    for service in ('core-worker', 'memory-worker', 'home-observer'):
        entries = []
        for path in (app.settings.state_dir / 'health').glob(service + '-*.enc'):
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 10000:
                    continue
                value = app.vault.open(path.read_text(), 'worker-heartbeat:' + service)
                age = now() - value['last_seen']
                if value.get('configuration') != configuration_id(app) or value['service'] != service or value['phase'] == 'stopped' or not 0 <= age <= max_age:
                    continue
                entries.append({key: value[key] for key in ('instance', 'phase', 'last_seen', 'last_cycle_at', 'cycles', 'error_type')})
            except Exception:
                continue
        result[service] = {'recent_heartbeat': bool(entries), 'instances': entries}
    return result
