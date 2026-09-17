"""验证固定模型后启动本地 whisper.cpp，不开放自动格式转换。"""
import hashlib,os
from pathlib import Path
root=Path(__file__).resolve().parents[1]
model=root/'state/models/ggml-small-q5_1.bin'
if not model.is_file() or model.is_symlink() or model.stat().st_size!=190085487:raise SystemExit('转写模型未完整安装')
with model.open('rb') as f:checksum=hashlib.file_digest(f,'sha256').hexdigest()
if checksum!='ae85e4a935d7a567bd102fe55afc16bb595bdb618e11b2fc7591bc08120411bb':raise SystemExit('转写模型 SHA256 不符')
binary=root/'state/whisper.cpp/build/bin/whisper-server'
if not binary.is_file():raise SystemExit('先运行 build_whisper.py')
os.execv(binary,[str(binary),'-m',str(model),'--host','127.0.0.1','--port','58085','-l','auto','-t','4'])
