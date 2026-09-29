"""运行持久通知箱、服务端到期提醒与 APNs 投递工作进程。"""
import asyncio
from homeai.notifications import main

if __name__ == '__main__':
    asyncio.run(main())
