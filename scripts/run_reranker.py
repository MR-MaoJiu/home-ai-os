"""运行只读本地模型的 Reranker，隔离依赖并拒绝网络出站。"""
import os,secrets,subprocess,sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
subprocess.run([sys.executable,str(root/'scripts/verify_provider_models.py'),'--manifest',str(root/'providers/models/bge-reranker-base.json'),'--root',str(root/'state/models/bge-reranker-base')],check=True)
token=root/'state/provider-secrets/reranker.token';token.parent.mkdir(parents=True,exist_ok=True)
if not token.exists():
    with os.fdopen(os.open(token,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:f.write(secrets.token_urlsafe(48))
if token.is_symlink() or token.stat().st_mode&0o077:raise SystemExit('凭据必须为 0600 普通文件')
env={key:os.environ[key] for key in ('PATH','HOME','TMPDIR','LANG') if key in os.environ}
env.update({'PYTHONPATH':str(root/'providers'),'HOMEAI_ADAPTER':'reranker','PROVIDER_SERVICE_TOKEN':token.read_text().strip(),'RERANKER_MODEL_PATH':str(root/'state/models/bge-reranker-base'),'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','HF_HUB_DISABLE_TELEMETRY':'1','HF_HUB_DISABLE_IMPLICIT_TOKEN':'1','HF_HOME':str(root/'state/reranker/hf-cache'),'TOKENIZERS_PARALLELISM':'false'})
python=root/'state/venvs/reranker/bin/python'
os.execve(python,[str(python),'-m','uvicorn','homeai_providers.service:app','--host','127.0.0.1','--port','8109','--no-access-log'],env)
