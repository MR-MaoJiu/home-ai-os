"""从本机私有配置启动成员专属邮件 Provider，不继承 Core 数据库与模型凭据。"""
import argparse
import json
import os
import sys
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--config', type=Path, required=True)
parser.add_argument('--port', type=int, default=8107)
args = parser.parse_args()
if args.config.is_symlink() or args.config.stat().st_mode & 0o077:
    raise SystemExit('邮件配置必须为非链接文件且权限为 0600')
config = json.loads(args.config.read_text())
allowed = {'MAIL_SUBJECT_ID', 'MAIL_USER', 'MAIL_PASSWORD', 'MAIL_FROM', 'MAIL_CA_FILE', 'MAIL_STATE_DIR', 'IMAP_HOST', 'IMAP_PORT', 'IMAP_SECURITY', 'SMTP_HOST', 'SMTP_PORT', 'SMTP_SECURITY', 'PROVIDER_SERVICE_TOKEN'}
if set(config) - allowed or not {'MAIL_SUBJECT_ID', 'MAIL_USER', 'MAIL_PASSWORD', 'MAIL_STATE_DIR', 'IMAP_HOST', 'SMTP_HOST', 'PROVIDER_SERVICE_TOKEN'} <= set(config):
    raise SystemExit('邮件配置字段缺失或包含不允许的字段')
if any(not isinstance(value, str) or not value for value in config.values()):
    raise SystemExit('邮件配置值必须为非空字符串')
if not Path(config['MAIL_STATE_DIR']).is_absolute():
    raise SystemExit('MAIL_STATE_DIR 必须为绝对路径，避免工作目录变化丢失去重账本')
if not 1024 <= args.port <= 65535:
    raise SystemExit('Provider 端口无效')
if any(config.get(name, 'tls') not in {'tls', 'starttls'} for name in ('IMAP_SECURITY', 'SMTP_SECURITY')):
    raise SystemExit('不允许明文邮件协议')
if len(config['PROVIDER_SERVICE_TOKEN']) < 32:
    raise SystemExit('Provider 服务凭据至少 32 字符')
environment = {name: os.environ[name] for name in ('PATH', 'HOME', 'TMPDIR', 'LANG') if name in os.environ}
environment.update(config)
environment.update(HOMEAI_ADAPTER='mail', PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'providers'), PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1')
os.execve(sys.executable, [sys.executable, '-m', 'uvicorn', 'homeai_providers.service:app', '--host', '127.0.0.1', '--port', str(args.port), '--no-access-log'], environment)
