"""迁移期间拒绝旧 HTTPS 中继；不将未实现的直连报告为成功。"""
import asyncio
import json
import time
from types import SimpleNamespace

from .config import Settings
from .crypto import Vault
from .remote import private_write, read_config


def write_migration_status(app):
    """只写入诊断，不联系平台、不申请租约、不启动转发进程。"""
    config = read_config(app)
    status = {
        'state': 'direct_not_ready' if config and config.get('enabled') else 'disabled',
        'transport_policy': 'direct_only',
        'relay_allowed': False,
        'process_running': False,
        'checked_at': time.time(),
    }
    private_write(app.settings.state_dir / 'remote-status.json', json.dumps(status))
    return status


async def main():
    settings = Settings()
    app = SimpleNamespace(settings=settings, vault=Vault.from_file(settings.master_key_file))
    while True:
        write_migration_status(app)
        await asyncio.sleep(5)


if __name__ == '__main__':
    asyncio.run(main())
