"""启动使用本地模型、关闭遥测的 Mem0；不传入 Core 数据库或云密钥。"""
import json,os,secrets
from pathlib import Path
root=Path(__file__).resolve().parents[1]
state=root/'state/mem0';state.mkdir(parents=True,exist_ok=True)
config=state/'config.json'
values={'llm':{'provider':'lmstudio','config':{'model':'Qwen3-0.6B-Q8_0.gguf','lmstudio_base_url':'http://127.0.0.1:58080/v1','api_key':'local-only'}},'embedder':{'provider':'lmstudio','config':{'model':'embeddinggemma-300M-Q8_0.gguf','lmstudio_base_url':'http://127.0.0.1:58081/v1','embedding_dims':768,'api_key':'local-only'}},'vector_store':{'provider':'qdrant','config':{'path':str(state/'vectors'),'collection_name':'homeai','embedding_model_dims':768,'on_disk':True}},'history_db_path':':memory:'}
if not config.exists():
    with os.fdopen(os.open(config,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:json.dump(values,f)
token=root/'state/provider-secrets/mem0.token';token.parent.mkdir(parents=True,exist_ok=True)
if not token.exists():
    with os.fdopen(os.open(token,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:f.write(secrets.token_urlsafe(48))
if token.is_symlink() or token.stat().st_mode&0o077 or config.is_symlink() or config.stat().st_mode&0o077:raise SystemExit('配置必须为 0600 普通文件')
env={key:os.environ[key] for key in ('PATH','HOME','TMPDIR','LANG') if key in os.environ}
env.update({'PYTHONPATH':str(root/'providers'),'HOMEAI_ADAPTER':'mem0','MEM0_TELEMETRY':'false','MEM0_DIR':str(state/'sdk'),'MEM0_EMBED_QUERY_PREFIX':'task: search result | query: ','MEM0_EMBED_DOCUMENT_PREFIX':'title: none | text: ','MEM0_CONFIG_FILE':str(config),'PROVIDER_SERVICE_TOKEN':token.read_text().strip(),'HF_HUB_OFFLINE':'1'})
python=root/'state/venvs/mem0/bin/python'
os.execve(python,[str(python),'-m','uvicorn','homeai_providers.service:app','--host','127.0.0.1','--port','8101','--no-access-log'],env)
