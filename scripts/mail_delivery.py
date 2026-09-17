"""本机核对邮件发送账本，不连接邮箱，也不发送任何邮件。"""
import argparse
import json
import os
from pathlib import Path
from fastapi import HTTPException
from homeai_providers.mail_recovery import inspect_delivery, resolve_delivery

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('action', choices=['inspect', 'resolve'])
parser.add_argument('--config', required=True, type=Path)
parser.add_argument('--invocation', required=True)
parser.add_argument('--decision', choices=['COMPLETED', 'NOT_EXECUTED', 'ABORT'])
parser.add_argument('--expected-hash')
parser.add_argument('--expected-revision', type=int)
parser.add_argument('--evidence-file', type=Path)
args = parser.parse_args()
if args.config.is_symlink() or args.config.stat().st_mode & 0o077:
    raise SystemExit('配置必须为权限 0600 的普通文件')
config = json.loads(args.config.read_text())
if not Path(config['MAIL_STATE_DIR']).is_absolute():
    raise SystemExit('MAIL_STATE_DIR 必须为绝对路径，避免工作目录变化丢失去重账本')
os.environ['MAIL_STATE_DIR'] = config['MAIL_STATE_DIR']
try:
    if args.action == 'inspect':
        result = inspect_delivery(config['MAIL_SUBJECT_ID'], args.invocation)
    else:
        if not args.decision or not args.expected_hash or args.expected_revision is None or not args.evidence_file:
            raise SystemExit('核对需要结论、inspect 返回的参数摘要和版本以及依据文件')
        if args.evidence_file.stat().st_size > 80000:
            raise SystemExit('核对依据文件过大')
        result = resolve_delivery(config['MAIL_SUBJECT_ID'], args.invocation, args.decision, args.expected_hash, args.evidence_file.read_text(), args.expected_revision)
    print(json.dumps(result, ensure_ascii=False))
except HTTPException as exc:
    raise SystemExit(str(exc.detail)) from None
