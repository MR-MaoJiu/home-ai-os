"""实际认证心跳、终止与失效，不用静态成功标记替代运行状态。"""
import asyncio
from homeai.heartbeat import Heartbeat,status


async def test_live_progress_and_stop(system):
    app=system[0].state
    async with Heartbeat(app,'core-worker') as heartbeat:
        heartbeat.progress()
        heartbeat.write()
        value=status(app)['core-worker']
        assert value['recent_heartbeat']
        assert value['instances'][0]['cycles']==1
        assert value['instances'][0]['phase']=='running'
        heartbeat.progress(RuntimeError('不写入诊断正文的内容'))
        heartbeat.write()
        assert status(app)['core-worker']['instances'][0]['error_type']=='RuntimeError'
    assert not status(app)['core-worker']['recent_heartbeat']


async def test_expired_or_tampered_heartbeat_is_not_alive(system):
    app=system[0].state
    heartbeat=Heartbeat(app,'memory-worker')
    heartbeat.write()
    await asyncio.sleep(0.03)
    assert not status(app,max_age=0.01)['memory-worker']['recent_heartbeat']
    heartbeat.path.write_text('{"phase":"running","last_seen":9999999999}')
    assert not status(app)['memory-worker']['recent_heartbeat']


async def test_other_database_configuration_is_not_accepted(system):
    app=system[0].state
    heartbeat=Heartbeat(app,'core-worker');heartbeat.progress();heartbeat.write()
    app.settings.database_url += '_different'
    assert not status(app)['core-worker']['recent_heartbeat']
