"""独立派生索引进程，避免文档批量导入阻塞聊天任务。"""
import asyncio,logging
from sqlalchemy import select
from .api import create_app
from .db import Principal
from .vector_index import reconcile
log=logging.getLogger('homeai.memory-worker')

async def main():
    app=create_app().state
    from .heartbeat import Heartbeat
    async with Heartbeat(app, "memory-worker") as heartbeat:
        while True:
            cycle_error=None
            with app.db() as db:users=[(p.id,p.household_id) for p in db.scalars(select(Principal))]
            for user,household in users:
                try:await reconcile(app,user,household)
                except Exception as exc:cycle_error=exc;log.warning('索引失败，保留事件重试：%s',type(exc).__name__)
                try:
                    from .knowledge import reconcile as reconcile_documents
                    await reconcile_documents(app,user,household)
                except Exception as exc:cycle_error=exc;log.warning('文档索引失败，等待重试：%s',type(exc).__name__)
                try:
                    from .derived_memory import reconcile as reconcile_derived
                    await reconcile_derived(app,user,household)
                except Exception as exc:cycle_error=exc;log.warning('派生索引失败，保留重建任务：%s',type(exc).__name__)
            heartbeat.progress(cycle_error)
            await asyncio.sleep(5)
if __name__=='__main__':asyncio.run(main())
