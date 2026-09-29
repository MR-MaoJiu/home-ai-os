"""运行用户自选的本机加密备份调度；没有显式启用时只等待，不创建备份。"""
import asyncio
from homeai.backup_settings import main

if __name__ == '__main__':
    asyncio.run(main())
