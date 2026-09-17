"""本机开发数据库加密备份；密钥与备份必须分开保管。"""
import argparse
import io
import os
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from homeai.backup import encrypt_stream, decrypt_stream, safe_extract

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('action', choices=['create', 'verify'])
parser.add_argument('--key', required=True, type=Path, help='独立 32 字节备份密钥文件')
parser.add_argument('--file', type=Path)
args = parser.parse_args()
key = args.key.read_bytes()
if len(key) != 32 or args.key.stat().st_mode & 0o077:
    raise SystemExit('备份密钥必须为 32 字节且权限为 0600')
if args.action == 'create':
    dump = subprocess.run(['docker','compose','--env-file',str(root/'.env.local'),'-f',str(root/'deploy/compose.dev.yml'),'exec','-T','postgres','pg_dump','-U','homeai_migrator','-d','homeai_runtime','-Fc'],check=True,capture_output=True).stdout
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer,mode='w:gz') as tar:
        info=tarfile.TarInfo('database.dump')
        info.size=len(dump)
        tar.addfile(info,io.BytesIO(dump))
        for name in ['blobs','deletions.jsonl']:
            path=root/'state'/name
            if path.exists():tar.add(path,arcname=name,recursive=True)
    buffer.seek(0)
    output=args.file or root/'state/backups'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.haib')
    output.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as dest:encrypt_stream(buffer,dest,key)
    print('加密备份已创建：'+str(output))
else:
    if not args.file:raise SystemExit('需要 --file')
    buffer=io.BytesIO()
    with args.file.open('rb') as src:decrypt_stream(src,buffer,key)
    buffer.seek(0)
    with tarfile.open(fileobj=buffer) as tar:
        if 'database.dump' not in tar.getnames():raise SystemExit('缺少数据库备份')
        print('备份认证与目录检查通过，条目数：'+str(len(tar.getmembers())))
