"""长任务不能阻塞新消息，重复调度不能并发执行同一个任务。"""
import asyncio
import pytest
from homeai.worker import cycle
from test_conversations import new_chat,send

@pytest.mark.asyncio
async def test_long_task_does_not_block_another_chat_or_duplicate_dispatch(system,alice,monkeypatch):
    chat=new_chat(alice)
    first=send(alice,chat,'长任务公开测试').json()['task_id']
    second=send(alice,chat,'你好').json()['task_id']
    entered=[];release=asyncio.Event()
    async def work(app,identifier,user):
        entered.append(identifier)
        await release.wait()
    monkeypatch.setattr('homeai.worker.run_task',work)
    app=system[0].state
    try:
        await asyncio.wait_for(cycle(app,concurrent=True),1)
        await asyncio.sleep(0)
        assert set(entered)=={first,second}
        await cycle(app,concurrent=True)
        await asyncio.sleep(0)
        assert len(entered)==2
    finally:
        release.set()
        await asyncio.gather(*app.running_tasks.values())
