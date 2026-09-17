"""运行独立 Graphiti，仅使用本地 Neo4j 与已配置模型。"""
import os,secrets
from pathlib import Path
root=Path(__file__).resolve().parents[1]
config=root/'.env.graphiti'
if not config.is_file() or config.is_symlink() or config.stat().st_mode&0o077:
    raise SystemExit('先运行 init_graphiti.py，配置必须为 0600')
values=dict(line.split('=',1) for line in config.read_text().splitlines() if line and not line.startswith('#'))
token=root/'state/provider-secrets/graphiti.token';token.parent.mkdir(parents=True,exist_ok=True)
if not token.exists():
    with os.fdopen(os.open(token,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:f.write(secrets.token_urlsafe(48))
if token.is_symlink() or token.stat().st_mode&0o077:raise SystemExit('Provider 凭据必须为 0600 普通文件')
env={key:os.environ[key] for key in ('PATH','HOME','TMPDIR','LANG') if key in os.environ}
env.update({'PYTHONPATH':str(root/'providers'),'HOMEAI_ADAPTER':'graphiti','PROVIDER_SERVICE_TOKEN':token.read_text().strip(),'GRAPHITI_TELEMETRY_ENABLED':'false','SEMAPHORE_LIMIT':'1','NEO4J_URI':'bolt://127.0.0.1:57687','NEO4J_USER':'neo4j','NEO4J_PASSWORD':values['HOMEAI_NEO4J_PASSWORD'],'LOCAL_MODEL_URL':'http://127.0.0.1:58082/v1','LOCAL_MODEL_NAME':'Qwen3-4B-Q4_K_M.gguf','LOCAL_EMBEDDING_URL':'http://127.0.0.1:58081/v1','LOCAL_EMBEDDING_MODEL':'embeddinggemma-300M-Q8_0.gguf','LOCAL_EMBEDDING_DIM':'768'})
python=root/'state/venvs/graphiti/bin/python'
os.execve(python,[str(python),'-m','uvicorn','homeai_providers.service:app','--host','127.0.0.1','--port','8102','--no-access-log'],env)
