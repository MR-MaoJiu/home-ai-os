"""校验官方固定权重后启动独立 4B Agent 模型，不替换原有生成服务。"""
import hashlib,os,shutil
from pathlib import Path
root=Path(__file__).resolve().parents[1]
path=root/'state/models/Qwen3-4B-Q4_K_M.gguf'
if not path.is_file() or path.is_symlink():raise SystemExit('需要完整本地 4B 权重')
if path.stat().st_size!=2497280256:raise SystemExit('模型大小不符，可能仍在下载')
with path.open('rb') as stream:checksum=hashlib.file_digest(stream,'sha256').hexdigest()
if checksum!='7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5':raise SystemExit('模型 SHA256 不符')
binary=shutil.which('llama-server')
if not binary:raise SystemExit('需要安装 llama.cpp')
os.execv(binary,[binary,'-m',str(path),'--host','127.0.0.1','--port','58082','-c','8192','--parallel','1','--alias','Qwen3-4B-Q4_K_M.gguf','--jinja','--reasoning-budget','0'])
