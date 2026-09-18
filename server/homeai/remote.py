"""可选远程连接客户端接口，不影响 Core 的本地运行。"""
import json
from fastapi import APIRouter,Request,Depends
from .security import authenticate,owner
from .private_files import private_write

router=APIRouter(prefix='/api/v1/remote',tags=['可选远程连接'])

def read_config(app):
    path=app.settings.state_dir/'remote-config.enc'
    return app.vault.open(path.read_text(),'remote-config') if path.exists() else None

@router.get('/status')
def status(request:Request,actor=Depends(authenticate)):
    owner(actor);config=read_config(request.app.state)
    status_file=request.app.state.settings.state_dir/'remote-status.json'
    runtime=json.loads(status_file.read_text()) if status_file.exists() else {'state':'agent_not_running'}
    import time
    if runtime.get('checked_at') and time.time()-runtime['checked_at']>180:
        runtime['state']='stale'
    from .certificate_store import runtime_status
    try:tls=runtime_status(request.app.state)
    except Exception as error:tls={'status':'unavailable','error_type':type(error).__name__}
    return {'configured':config is not None,'enabled':False,'previously_enabled':bool(config and config.get('enabled')),
            'transport_policy':'direct_only','direct_ready':False,'relay_allowed':False,
            'migration_required':config is not None,'url':config.get('url') if config else None,'runtime':{'state':'direct_not_ready'},'legacy_runtime':runtime,
            'managed_https_port':request.app.state.settings.managed_https_port,'tls':tls}

@router.post('/disable')
def disable(request:Request,actor=Depends(authenticate)):
    owner(actor);app=request.app.state;config=read_config(app)
    if config:
        config['enabled']=False;private_write(app.settings.state_dir/'remote-config.enc',app.vault.seal(config,'remote-config'))
    return {'enabled':False}
