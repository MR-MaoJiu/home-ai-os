"""校验预下载模型；不下载、不修复、不自动接受上游变化。"""
import argparse
import hashlib
import json
from pathlib import Path

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--manifest', type=Path, required=True)
parser.add_argument('--root', type=Path, required=True)
args=parser.parse_args()
manifest=json.loads(args.manifest.read_text())
root=args.root.resolve()
for item in manifest['files']:
    path=root/item['path']
    if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
        raise SystemExit('模型文件缺失或路径无效：'+item['path'])
    if path.stat().st_size!=item['bytes']:
        raise SystemExit('模型文件大小不符：'+item['path'])
    with path.open('rb') as stream:checksum=hashlib.file_digest(stream,'sha256').hexdigest()
    if checksum!=item['sha256']:raise SystemExit('模型校验不符：'+item['path'])
print(f"已核对 {len(manifest['files'])} 个模型与配置文件")
