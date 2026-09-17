"""生成项目私有开发配置，不覆盖已有文件、不输出秘密。"""
import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
state = root / "state"
state.mkdir(exist_ok=True)

def private_write(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(content)

if not (root / ".env.local").exists():
    admin = secrets.token_urlsafe(32)
    app = secrets.token_urlsafe(32)
    private_write(root / ".env.local", f"HOMEAI_DB_ADMIN_PASSWORD={admin}\nHOMEAI_DB_APP_PASSWORD={app}\nHOMEAI_DATABASE_URL=postgresql+psycopg://homeai_app:{app}@127.0.0.1:55432/homeai_runtime\nHOMEAI_OPA_URL=http://127.0.0.1:58181\nHOMEAI_NATS_URL=nats://127.0.0.1:54222\nHOMEAI_STATE_DIR=./state\nHOMEAI_MASTER_KEY_FILE=./state/master.key\n")
    print("已创建私有开发配置")
else:
    print("保留已有开发配置")
