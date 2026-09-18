"""校验固定 MLX 模型，使用过滤环境启动仅本机可达的开发服务。"""
import os
import platform
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
if platform.system() != 'Darwin' or platform.machine() != 'arm64':
    raise SystemExit('此运行配置要求 Apple Silicon Mac')
model = root / 'state/models/mlx-qwen3-4b'
subprocess.run([
    sys.executable, str(root / 'scripts/verify_provider_models.py'),
    '--manifest', str(root / 'providers/models/mlx-qwen3-4b.json'),
    '--root', str(model),
], check=True)
env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
           HF_HUB_DISABLE_TELEMETRY='1', HF_HUB_DISABLE_IMPLICIT_TOKEN='1',
           HF_HOME=str(root / 'state/mlx/hf-cache'))
python = root / 'state/venvs/mlx/bin/python'
os.execve(python, [str(python), str(root / 'scripts/mlx_server.py')], env)
