"""运行独立的本机 Docling 服务；凭据不经过命令行参数或日志。"""
import os
from pathlib import Path
import secrets
import sys
import subprocess

root = Path(__file__).resolve().parents[1]
python = root / 'state/venvs/docling/bin/python'
if not python.exists():
    raise SystemExit('请先按 README 安装 Docling 独立环境')
subprocess.run([sys.executable, str(root / 'scripts/verify_provider_models.py'), '--manifest', str(root / 'providers/models/docling-2.128.0.json'), '--root', str(root / 'state/models/docling')], check=True)
token_path = root / 'state/provider-secrets/docling.token'
token_path.parent.mkdir(parents=True, exist_ok=True)
if not token_path.exists():
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(secrets.token_urlsafe(48))
if token_path.is_symlink() or token_path.stat().st_mode & 0o077:
    raise SystemExit('Provider 凭据文件必须是 0600 私有普通文件')
(root / 'state/models/docling').mkdir(parents=True, exist_ok=True)
# 只传递运行所需环境；Core 数据库地址和模型密钥不进入 Provider。
env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
env.update({'PYTHONPATH': str(root / 'providers'), 'HOMEAI_ADAPTER': 'docling', 'PROVIDER_SERVICE_TOKEN': token_path.read_text().strip(), 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'DOCLING_ARTIFACTS_PATH': str(root / 'state/models/docling')})
os.execve(python, [str(python), '-m', 'uvicorn', 'homeai_providers.service:app', '--host', '127.0.0.1', '--port', '8103', '--no-access-log'], env)
