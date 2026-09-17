"""启动有身份验证的本机转写桥接服务，不传入 Core 数据库凭据。"""
import os,secrets,sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
token=root/'state/provider-secrets/whisper.token';token.parent.mkdir(parents=True,exist_ok=True)
if not token.exists():
    with os.fdopen(os.open(token,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:f.write(secrets.token_urlsafe(48))
if token.is_symlink() or token.stat().st_mode&0o077:raise SystemExit('Provider 凭据必须为 0600 普通文件')
env={key:os.environ[key] for key in ('PATH','HOME','TMPDIR','LANG') if key in os.environ}
env.update({'PYTHONPATH':str(root/'providers'),'HOMEAI_ADAPTER':'whisper','PROVIDER_SERVICE_TOKEN':token.read_text().strip(),'WHISPER_URL':'http://127.0.0.1:58085'})
os.execve(sys.executable,[sys.executable,'-m','uvicorn','homeai_providers.service:app','--host','127.0.0.1','--port','8105','--no-access-log'],env)
