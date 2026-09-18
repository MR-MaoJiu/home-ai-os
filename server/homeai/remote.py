"""可选远程连接客户端接口，不影响 Core 的本地运行。"""
import base64,json,os,secrets
from pathlib import Path
from urllib.parse import urlparse
from fastapi import APIRouter,Request,Depends,HTTPException
from pydantic import BaseModel,ConfigDict,Field
from cryptography.hazmat.primitives import serialization
from .security import authenticate,owner

router=APIRouter(prefix='/api/v1/remote',tags=['可选远程连接'])

def private_write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp-'+secrets.token_hex(6))
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as file:file.write(value)
    os.replace(temp,path)

from .server_identity import identity

def read_config(app):
    path=app.settings.state_dir/'remote-config.enc'
    return app.vault.open(path.read_text(),'remote-config') if path.exists() else None

@router.get('/request-code')
def request_code(request:Request,actor=Depends(authenticate)):
    owner(actor);server_id,key=identity(request.app.state)
    value={'schema_version':'1.0','server_id':server_id,'public_key':key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()}
    return {'request_code':base64.b64encode(json.dumps(value).encode()).decode(),'server_id':server_id}

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
            'migration_required':config is not None,'url':config.get('url') if config else None,'runtime':runtime,
            'managed_https_port':request.app.state.settings.managed_https_port,'tls':tls}

class Binding(BaseModel):
    model_config=ConfigDict(extra='forbid')
    portal_url:str=Field(default='https://homeai-connect.pintheworld.cn',max_length=300)
    binding_code:str=Field(min_length=20,max_length=10000)
    local_https_port:int|None=Field(default=None,ge=1024,le=65535)

@router.post('/bind')
async def bind(body:Binding,request:Request,actor=Depends(authenticate)):
    owner(actor);app=request.app.state
    if body.local_https_port is not None and body.local_https_port!=app.settings.managed_https_port:
        raise HTTPException(422,'仅允许服务器配置的 Home AI 受管 HTTPS 端口')
    endpoint=urlparse(body.portal_url)
    if endpoint.scheme!='https' or not endpoint.hostname or endpoint.username or endpoint.query or endpoint.fragment:raise HTTPException(422,'平台地址必须是 HTTPS')
    try:data=json.loads(base64.b64decode(body.binding_code,validate=True));token=data['binding_token'];instance_id=data['id']
    except Exception:raise HTTPException(422,'绑定码无效') from None
    target=urlparse(str(data.get('url','')))
    if target.scheme!='https' or not target.hostname or target.username or target.query or target.fragment:
        raise HTTPException(422,'绑定地址必须是无凭据的 HTTPS 地址')
    raise HTTPException(409,'旧版中继绑定已停用；纯直连协商尚未完成，不能建立远程连接')

@router.post('/use-managed-https')
def use_managed_https(request:Request,actor=Depends(authenticate)):
    owner(actor);app=request.app.state;config=read_config(app)
    if not config:raise HTTPException(409,'尚未绑定远程实例')
    raise HTTPException(409,'中继迁移入口已停用；HTTPS 证书就绪不代表 P2P 直连就绪')

@router.post('/disable')
def disable(request:Request,actor=Depends(authenticate)):
    owner(actor);app=request.app.state;config=read_config(app)
    if config:
        config['enabled']=False;private_write(app.settings.state_dir/'remote-config.enc',app.vault.seal(config,'remote-config'))
    return {'enabled':False}

@router.post('/enable')
def enable(request:Request,actor=Depends(authenticate)):
    owner(actor);app=request.app.state;config=read_config(app)
    if not config:raise HTTPException(409,'尚未绑定远程实例')
    raise HTTPException(409,'旧版中继已停用；纯直连尚未就绪，禁止回退到中继')
