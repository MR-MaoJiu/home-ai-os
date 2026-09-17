"""运行只读本地模型的 FunASR，隔离依赖并拒绝网络出站。"""
import os,secrets,subprocess,sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
subprocess.run([sys.executable,str(root/'scripts/verify_provider_models.py'),'--manifest',str(root/'providers/models/sensevoice-small.json'),'--root',str(root/'state/models/sensevoice')],check=True)
token=root/'state/provider-secrets/funasr.token';token.parent.mkdir(parents=True,exist_ok=True)
if not token.exists():
    with os.fdopen(os.open(token,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:f.write(secrets.token_urlsafe(48))
if token.is_symlink() or token.stat().st_mode&0o077:raise SystemExit('凭据必须为 0600 普通文件')
env={key:os.environ[key] for key in ('PATH','HOME','TMPDIR','LANG') if key in os.environ}
env.update({'PYTHONPATH':str(root/'providers'),'HOMEAI_ADAPTER':'funasr','PROVIDER_SERVICE_TOKEN':token.read_text().strip(),'FUNASR_MODEL_PATH':str(root/'state/models/sensevoice'),'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','MODELSCOPE_OFFLINE':'1','HF_HOME':str(root/'state/funasr/hf-cache'),'MODELSCOPE_CACHE':str(root/'state/funasr/modelscope-cache'),'NUMBA_CACHE_DIR':str(root/'state/funasr/numba-cache')})
python=root/'state/venvs/funasr/bin/python'
os.execve(python,[str(python),'-m','uvicorn','homeai_providers.service:app','--host','127.0.0.1','--port','8104','--no-access-log'],env)
