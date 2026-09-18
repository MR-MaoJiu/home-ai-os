"""本机开发数据库加密备份；密钥与备份必须分开保管。"""
import argparse
import io
import json
import hashlib
import os
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from homeai.backup import encrypt_stream, decrypt_stream, safe_extract, verify_archive

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('action', choices=['create', 'verify'])
parser.add_argument('--key', required=True, type=Path, help='独立 32 字节备份密钥文件')
parser.add_argument('--file', type=Path)
parser.add_argument("--allow-legacy", action="store_true", help="仅验证显式接受的旧版无清单归档")
args = parser.parse_args()
key = args.key.read_bytes()
if len(key) != 32 or args.key.stat().st_mode & 0o077:
    raise SystemExit('备份密钥必须为 32 字节且权限为 0600')
if args.action == 'create':
    dump = subprocess.run(['docker','compose','--env-file',str(root/'.env.local'),'-f',str(root/'deploy/compose.dev.yml'),'exec','-T','postgres','pg_dump','-U','homeai_migrator','-d','homeai_runtime','-Fc'],check=True,capture_output=True).stdout
    buffer = io.BytesIO()
    manifest = {'format_version': 2, 'created_at': datetime.now(timezone.utc).isoformat(), 'files': {}}
    with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
        def add(name, content):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(content))
            manifest['files'][name] = {'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
        add('database.dump', dump)
        for name in ['blobs', 'deletions.jsonl', 'server-identity.enc']:
            path = root / 'state' / name
            if path.is_symlink():
                raise SystemExit('备份源不能是符号链接')
            if not path.exists():
                continue
            paths = sorted(path.rglob('*')) if path.is_dir() else [path]
            for item in paths:
                if item.is_symlink():
                    raise SystemExit('备份源不能包含符号链接')
                if item.is_file():
                    add(item.relative_to(root / 'state').as_posix(), item.read_bytes())
        raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
        info = tarfile.TarInfo('manifest.json'); info.size = len(raw); info.mode = 0o600
        tar.addfile(info, io.BytesIO(raw))
    verify_archive(buffer)
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
    result = verify_archive(buffer, allow_legacy=args.allow_legacy)
    print('归档验证结果：' + json.dumps(result, ensure_ascii=False))
