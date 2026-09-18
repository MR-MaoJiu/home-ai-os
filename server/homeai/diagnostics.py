"""只读部署检查，不回显连接地址、凭据或业务内容。"""
import asyncio
import httpx
import nats
from sqlalchemy import text
from .db import Base, now


def database_check(app):
    try:
        with app.db() as db:
            if db.get_bind().dialect.name != 'postgresql':
                return {'ok': False, 'reason': '实际部署需要 PostgreSQL'}
            db.execute(text("SET LOCAL statement_timeout = '2s'"))
            role = db.execute(text('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user')).one()
            names = [table.name for table in Base.metadata.tables.values() if 'owner_id' in table.c]
            rows = db.execute(text('SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class WHERE relnamespace=current_schema()::regnamespace AND relname=ANY(:names)'), {'names': names}).all()
            safe_role = not role.rolsuper and not role.rolbypassrls
            rls = len(rows) == len(names) and all(row.relrowsecurity and row.relforcerowsecurity for row in rows)
            return {'ok': safe_role and rls, 'connected': True, 'restricted_role': safe_role,
                    'forced_rls': rls, 'reason': '数据库与权限边界通过' if safe_role and rls else '数据库角色或 RLS 不满足部署要求'}
    except Exception as exc:
        return {'ok': False, 'reason': '数据库检查失败', 'error_type': type(exc).__name__}


async def opa_check(app, actor):
    try:
        async with httpx.AsyncClient(timeout=2, trust_env=False, follow_redirects=False) as client:
            # 同时验证允许与拒绝，避免空策略或全放行被当成就绪。
            for risk, approved, expected in [(1, False, True), (3, False, False), (4, True, False)]:
                response = await client.post(app.settings.opa_url.rstrip('/') + '/v1/data/homeai/allow',
                    json={'input': {'actor': actor.__dict__, 'capability': 'diagnostic.check', 'risk': risk, 'approved': approved}})
                response.raise_for_status()
                if response.json().get('result') is not expected:
                    return {'ok': False, 'reason': '策略决策与安全约束不一致'}
        return {'ok': True, 'reason': '策略允许与拒绝检查通过'}
    except Exception as exc:
        return {'ok': False, 'reason': '策略服务不可用', 'error_type': type(exc).__name__}


async def nats_check(app):
    connection = None
    try:
        connection = await nats.connect(app.settings.nats_url, connect_timeout=2, allow_reconnect=False)
        js = connection.jetstream(timeout=2)
        await js.account_info()
        try:
            stream = await js.stream_info(app.settings.event_stream)
        except nats.js.errors.NotFoundError:
            return {'ok': False, 'reason': 'JetStream 可用，但事件流尚未由 worker 初始化'}
        required = app.settings.event_subject_prefix + '.>'
        if required not in stream.config.subjects:
            return {'ok': False, 'reason': '事件流主题配置不匹配'}
        return {'ok': True, 'reason': 'JetStream 与事件流可用', 'messages': stream.state.messages}
    except Exception as exc:
        return {'ok': False, 'reason': '事件服务检查失败', 'error_type': type(exc).__name__}
    finally:
        if connection:
            await connection.close()


async def inspect(app, actor):
    database, opa, events = await asyncio.gather(asyncio.to_thread(database_check, app), opa_check(app, actor), nats_check(app))
    try:
        value = {'purpose': 'readiness'}
        vault_ok = app.vault.open(app.vault.seal(value, 'readiness'), 'readiness') == value
    except Exception:
        vault_ok = False
    from .remote import read_config
    try:
        remote = read_config(app) is not None
    except Exception:
        remote = False
    checks = {'database': database, 'policy': opa, 'events': events,
              'encryption': {'ok': vault_ok, 'reason': '信封加密往返检查通过' if vault_ok else '加密检查失败'}}
    from .heartbeat import status as worker_status
    workers = worker_status(app)
    required = ['core-worker', 'memory-worker']
    try:
        import json
        from .db import Provider
        from sqlalchemy import select
        with app.db() as db:
            if any(json.loads(provider.manifest).get('home_events') for provider in db.scalars(select(Provider).where(Provider.enabled.is_(True)))):
                required.append('home-observer')
    except Exception:
        required.append('home-observer')
    ready_workers = all(any(item['phase'] == 'running' for item in workers[name]['instances']) for name in required)
    dependencies_ready = all(item['ok'] for item in checks.values())
    checks['workers'] = {'ok': ready_workers, 'reason': '必需执行器近期有正常循环心跳' if ready_workers else '必需执行器未启动、心跳过期、初始化中或最近循环失败'}
    return {'database': database.get('connected', False), 'master_key_loaded': vault_ok,
        'environment': app.settings.environment, 'production_sandbox_verified': False,
        'remote_configured': remote, 'core_dependencies_ready': dependencies_ready, 'runtime_ready': dependencies_ready and ready_workers, 'workers': workers,
        'checks': checks, 'checked_at': now(), 'unverified': ['任务与 Provider 实际执行', '模型推理', '生产沙箱', '备份恢复', '真机与家庭部署']}
