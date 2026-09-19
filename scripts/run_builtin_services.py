"""受控内置服务部署器。仅执行固定命令；不要以 root 运行。"""
import hashlib,os,shutil,subprocess,sys,time,signal
from pathlib import Path
import httpx
from sqlalchemy import select,text
from homeai.config import Settings
from homeai.db import database,BuiltinDeployment,Provider,now
from homeai.contracts import ProviderManifest
from homeai.builtins import MODELS

root=Path(__file__).resolve().parents[1]
os.chdir(root)
settings=Settings()
if settings.environment=='production':raise SystemExit('此部署器尚未通过生产沙箱验收')
_,factory=database(settings.database_url)
children={}
service_env={key:os.environ[key] for key in ('PATH','HOME','TMPDIR','LANG','DOCKER_HOST','DOCKER_CONTEXT') if key in os.environ}

def download(model):
    directory=settings.state_dir/'models';directory.mkdir(parents=True,exist_ok=True)
    target=directory/model['file']
    def valid(path):
        if path.is_symlink() or not path.is_file() or path.stat().st_size!=model['bytes']:return False
        with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()==model['sha256']
    if valid(target):return target
    if target.exists():raise ValueError('已存在权重校验失败，请在服务器检查，未覆盖文件')
    if shutil.disk_usage(directory).free<model['bytes']+512*1024*1024:raise ValueError('磁盘可用空间不足')
    temporary=directory/(model['file']+'.download')
    try:
        with httpx.stream('GET',f"https://huggingface.co/{model['repo']}/resolve/{model['revision']}/{model['file']}",follow_redirects=True,timeout=60,trust_env=False) as response:
            response.raise_for_status()
            with os.fdopen(os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600),'wb') as out:
                size=0
                for chunk in response.iter_bytes(1024*1024):
                    size+=len(chunk)
                    if size>model['bytes']:raise ValueError('下载文件超过固定大小')
                    out.write(chunk)
                    (settings.state_dir/'builtin-worker.heartbeat').touch()
        if not valid(temporary):raise ValueError('下载文件 SHA256 校验失败')
        temporary.replace(target)
    finally:temporary.unlink(missing_ok=True)
    return target

def stop(identifier):
    child=children.pop(identifier,None)
    if child:
        child.terminate()
        try:child.wait(timeout=15)
        except subprocess.TimeoutExpired:child.kill();child.wait()

def launch(identifier,variant):
    if identifier=='local-model':
        binary=shutil.which('llama-server')
        if not binary:raise ValueError('未安装 llama.cpp；请在服务器安装 llama-server 后重试')
        model=MODELS[variant];path=download(model)
        manifest=ProviderManifest(id='builtin.local-model',version=model['revision'],adapter='openai',endpoint='http://127.0.0.1:58100/v1',model=model['file'],allowed_hosts=['127.0.0.1'],capabilities={'model.generate@v1':'chat'})
        command=[binary,'-m',str(path),'--host','127.0.0.1','--port','58100','-c','8192','--parallel','1','--alias',model['file'],'--jinja','--reasoning-budget','0']
        # 不接管同端口的不明进程。
        import socket
        with socket.socket() as sock:sock.bind(('127.0.0.1',58100))
        log=open(settings.state_dir/'builtin-model.log','ab')
        children[identifier]=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=log,env=service_env)
        log.close()
        endpoint=manifest.endpoint+'/models'
    else:
        if not shutil.which('docker'):raise ValueError('未安装 Docker，请先启动 Docker 服务')
        subprocess.run([sys.executable,'scripts/init_searxng.py'],check=True,capture_output=True,timeout=30,env=service_env)
        subprocess.run(['docker','compose','-f','deploy/compose.searxng.yml','up','-d'],check=True,capture_output=True,timeout=600,env=service_env)
        manifest=ProviderManifest.model_validate_json((root/'providers/manifests/searxng.json').read_text());manifest.id='builtin.web-search'
        endpoint=manifest.endpoint+'/config'
    for _ in range(90):
        (settings.state_dir/'builtin-worker.heartbeat').touch()
        try:
            response=httpx.get(endpoint,timeout=3,trust_env=False);response.raise_for_status()
            value=response.json()
            if identifier=='local-model' and not any(m.get('id')==manifest.model for m in value.get('data',[])):raise ValueError('模型端点身份不符')
            if identifier=='web-search' and '274b63b67' not in value.get('version',''):raise ValueError('搜索引擎版本不符')
            return manifest
        except (httpx.HTTPError,ValueError):time.sleep(1)
    stop(identifier)
    raise ValueError('启动超时，服务未通过健康检查')

def main():
    settings.state_dir.mkdir(parents=True,exist_ok=True)
    with factory.kw['bind'].connect() as lock:
        if not lock.scalar(text('SELECT pg_try_advisory_lock(804220)')):raise SystemExit('已有内置服务部署器运行')
        # 进程重启后恢复已启用服务，失败项需用户明确重试。
        with factory() as db:
            for row in db.scalars(select(BuiltinDeployment).where(BuiltinDeployment.enabled.is_(True),BuiltinDeployment.status!='failed')):row.status='queued'
            db.commit()
        while True:
            (settings.state_dir/'builtin-worker.heartbeat').touch()
            with factory() as db:jobs=[(r.id,r.variant,r.enabled,r.updated_at) for r in db.scalars(select(BuiltinDeployment).where(BuiltinDeployment.status=='queued'))]
            for identifier,variant,enabled,revision in jobs:
                stop(identifier)
                with factory() as db:
                    row=db.get(BuiltinDeployment,identifier)
                    if row.updated_at!=revision:continue
                    row.status='starting';db.commit()
                error=None;manifest=None
                try:
                    if enabled:manifest=launch(identifier,variant)
                except Exception as exc:error=str(exc) if isinstance(exc,ValueError) else type(exc).__name__+'：请检查服务器运行环境'
                with factory() as db:
                    row=db.get(BuiltinDeployment,identifier)
                    if row.updated_at!=revision:continue
                    row.status='failed' if error else 'ready' if enabled else 'disabled';row.error=error
                    if manifest:
                        provider=db.get(Provider,manifest.id)
                        if not provider:provider=Provider(id=manifest.id,manifest=manifest.model_dump_json());db.add(provider)
                        provider.manifest=manifest.model_dump_json();provider.enabled=True;provider.health='ready'
                    db.commit()
            for identifier,child in list(children.items()):
                if child.poll() is not None:
                    children.pop(identifier)
                    with factory() as db:
                        row=db.get(BuiltinDeployment,identifier);row.status='failed';row.error='模型进程已退出，请检查内存与服务器日志'
                        provider=db.get(Provider,'builtin.'+identifier)
                        if provider:provider.enabled=False;provider.health='offline'
                        db.commit()
            time.sleep(2)

def shutdown(*_):raise SystemExit(0)

if __name__=='__main__':
    signal.signal(signal.SIGTERM,shutdown)
    try:main()
    finally:
        for identifier in list(children):stop(identifier)
        (settings.state_dir/'builtin-worker.heartbeat').unlink(missing_ok=True)
