"""公开验收脚本只使用既有状态，规划持锁先于可见提交；不调用真实模型或设备。"""
from contextlib import contextmanager
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from homeai import android_workspace as android
from homeai.db import Task
from test_android_workspace import workspace

spec = importlib.util.spec_from_file_location('android_launch_public_check', Path(__file__).resolve().parents[2] / 'scripts/check_android_launch.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


@pytest.fixture
async def public_settings(workspace, monkeypatch):
    app, actor, executed = workspace
    with app.db() as db:
        row = android.get_binding(app, db, actor)
        row.packages = '["com.android.settings"]'
        db.commit()
    monkeypatch.setattr(android.android_adb, '_run', AsyncMock(return_value=b'package:/public/base.apk'))
    monkeypatch.setattr(android, 'foreground_package', AsyncMock(return_value='com.android.settings'))
    return app, actor, executed


class Connection:
    def __init__(self, calls): self.calls = calls
    def scalar(self, statement, parameters): self.calls.append(('lock', parameters['key'])); return True
    def execute(self, statement, parameters): self.calls.append(('unlock', parameters['key']))
    def commit(self): self.calls.append(('commit',))
    def close(self): self.calls.append(('close',))
    def invalidate(self): self.calls.append(('invalidate',))


def test_postgres_guard_matches_runtime_key_and_releases_after_failure():
    import hashlib
    calls = []
    engine = SimpleNamespace(dialect=SimpleNamespace(name='postgresql'), connect=lambda: Connection(calls))
    identifier = 'public-task'
    expected = int.from_bytes(hashlib.sha256(('task:' + identifier).encode()).digest()[:8], 'big', signed=True)
    with pytest.raises(RuntimeError):
        with helper.task_guard(engine, identifier):
            calls.append(('visible_task_commit',))
            raise RuntimeError('公开测试规划异常')
    assert calls == [('lock', expected), ('commit',), ('visible_task_commit',), ('unlock', expected), ('commit',), ('close',)]


async def test_helper_locks_before_visible_executing_and_keeps_lock_through_terminal(public_settings, monkeypatch):
    app, actor, executed = public_settings
    state = {'locked': False}
    events = []
    @contextmanager
    def guard(engine, identifier):
        state['locked'] = True
        events.append('lock')
        try: yield
        finally:
            events.append('unlock')
            state['locked'] = False
    monkeypatch.setattr(helper, 'task_guard', guard)
    factory = app.db
    def traced_factory():
        db = factory()
        commit = db.commit
        def traced_commit():
            for item in list(db.dirty) + list(db.new):
                if isinstance(item, Task) and item.idempotency_key.startswith('android-public-planning-'):
                    assert state['locked']
                    assert item.status in {'EXECUTING', 'SUCCEEDED', 'FAILED', 'CANCELED'}
                    events.append(item.status)
            return commit()
        db.commit = traced_commit
        return db
    app.db = traced_factory
    monkeypatch.setattr(helper, 'resolve_role', lambda *_: SimpleNamespace(id='public-model', cloud=True))
    async def cloud(app_arg, db, caller, manifest, task, body, arguments, call_key):
        assert state['locked']
        with factory() as independent:
            assert independent.get(Task, task.id).status == 'EXECUTING'
        assert body['record_ids'] == [] and len(arguments['messages']) == 2
        assert [item['function']['name'] for item in arguments['tools']] == ['open_android_app']
        return {'choices': [{'message': {'tool_calls': [{'function': {'name': 'open_android_app',
            'arguments': '{"package":"com.android.settings","purpose":"公开测试"}'}}]}}]}
    monkeypatch.setattr(helper, 'cloud_invoke', cloud)
    result = await helper.check(app, actor.user_id, actor.device_id)
    assert result['mode'] == 'model_and_core' and result['model_selected_tool'] == 'open_android_app'
    assert result['execution_status'] == 'SUCCEEDED' and result['workspace_released']
    assert events.index('lock') < events.index('EXECUTING') < events.index('SUCCEEDED') < events.index('unlock')
    executed.assert_awaited_once()


async def test_resolve_role_failure_commits_failed_without_unknown_status_or_device_execution(public_settings, monkeypatch):
    app, actor, executed = public_settings
    def unavailable(*_): raise HTTPException(409, '公开测试模型未配置')
    monkeypatch.setattr(helper, 'resolve_role', unavailable)
    cloud = AsyncMock()
    monkeypatch.setattr(helper, 'cloud_invoke', cloud)
    with pytest.raises(HTTPException) as error:
        await helper.check(app, actor.user_id, actor.device_id)
    assert error.value.status_code == 409
    with app.db() as db:
        rows = db.scalars(select(Task)).all()
        assert len(rows) == 1 and rows[0].status == 'FAILED'
        assert rows[0].error == '公开测试模型未配置'
    cloud.assert_not_awaited(); executed.assert_not_awaited()


async def test_core_only_never_calls_model_and_reports_no_model_success(public_settings, monkeypatch):
    app, actor, executed = public_settings
    def forbidden(*_): raise AssertionError('Core-only 不能解析或调用模型')
    monkeypatch.setattr(helper, 'resolve_role', forbidden)
    cloud = AsyncMock(side_effect=AssertionError('Core-only 不能调用模型'))
    monkeypatch.setattr(helper, 'cloud_invoke', cloud)
    result = await helper.check(app, actor.user_id, actor.device_id, skip_model=True)
    assert result['mode'] == 'core_only'
    assert result['model_selected_tool'] is None and result['provider_id'] is None and result['planning_task_id'] is None
    assert result['foreground_verified'] and result['workspace_released'] and result['execution_status'] == 'SUCCEEDED'
    with app.db() as db:
        assert len(db.scalars(select(Task)).all()) == 1
    cloud.assert_not_awaited(); executed.assert_awaited_once()


def test_real_postgres_guard_excludes_worker_and_releases_lock():
    import hashlib
    import os
    import uuid
    from sqlalchemy import text
    from homeai.config import Settings
    from homeai.db import database
    if os.environ.get('HOMEAI_INTEGRATION') != '1':
        pytest.skip('需要独立 homeai_test 数据库，只验证会话锁，不读写业务行')
    settings = Settings()
    engine, _ = database(settings.database_url.rsplit('/', 1)[0] + '/homeai_test')
    identifier = str(uuid.uuid4())
    key = int.from_bytes(hashlib.sha256(('task:' + identifier).encode()).digest()[:8], 'big', signed=True)
    try:
        with engine.connect() as contender:
            with helper.task_guard(engine, identifier):
                assert contender.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': key}) is False
                contender.commit()
            assert contender.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': key}) is True
            contender.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': key})
            contender.commit()
    finally:
        engine.dispose()
