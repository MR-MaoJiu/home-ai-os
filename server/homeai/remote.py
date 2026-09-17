"""可选远程连接客户端接口，不影响 Core 的本地运行。"""
import base64,json,os,secrets
from pathlib import Path
from urllib.parse import urlparse
import httpx
from fastapi import APIRouter,Request,Depends,HTTPException
from pydantic import BaseModel,ConfigDict,Field
from cryptography.hazmat.primitives import serialization,hashes
from cryptography.hazmat.primitives.asymmetric import ec
from .security import authenticate,owner
from .db import uid

router=APIRouter(prefix='/api/v1/remote',tags=['可选远程连接'])

def private_write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp-'+secrets.token_hex(6))
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as file:file.write(value)
    os.replace(temp,path)

def identity(app):
    path=app.settings.state_dir/'server-identity.enc'
    if not path.exists():
        key=ec.generate_private_key(ec.SECP256R1())
        value={'server_id':uid(),'private_key':key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()}
        # 首次创建使用排他打开，避免并发初始化覆盖服务器身份。
        try:
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as f:f.write(app.vault.seal(value,'server-identity'))
        except FileExistsError:pass
    value=app.vault.open(path.read_text(),'server-identity')
    key=serialization.load_pem_private_key(value['private_key'].encode(),password=None)
    return value['server_id'],key

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
    return {'configured':config is not None,'enabled':bool(config and config.get('enabled')),'url':config.get('url') if config else None,'runtime':runtime}

class Binding(BaseModel):
    model_config=ConfigDict(extra='forbid')
    portal_url:str=Field(default='https://homeai-connect.pintheworld.cn',max_length=300)
    binding_code:str=Field(min_length=20,max_length=10000)
    local_https_port:int=Field(default=58443,ge=1024,le=65535)

@router.post('/bind')
async def bind(body:Binding,request:Request,actor=Depends(authenticate)):
    owner(actor);app=request.app.state
    endpoint=urlparse(body.portal_url)
    if endpoint.scheme!='https' or not endpoint.hostname or endpoint.username or endpoint.query or endpoint.fragment:raise HTTPException(422,'平台地址必须是 HTTPS')
    try:data=json.loads(base64.b64decode(body.binding_code,validate=True));token=data['binding_token'];instance_id=data['id']
    except Exception:raise HTTPException(422,'绑定码无效') from None
    _,key=identity(app)
    signature=base64.b64encode(key.sign(('homeai-connect-bind:'+token).encode(),ec.ECDSA(hashes.SHA256()))).decode()
    async with httpx.AsyncClient(timeout=20,trust_env=False,follow_redirects=False) as client:
        ca=await client.get(body.portal_url.rstrip('/')+'/api/public/relay-ca');ca.raise_for_status()
        response=await client.post(body.portal_url.rstrip('/')+'/api/agent/claim',json={'instance_id':instance_id,'token':token,'signature':signature});response.raise_for_status()
    credential=response.json()['credential']
    config={'portal_url':body.portal_url.rstrip('/'),'instance_id':instance_id,'credential':credential,'url':data['url'],'local_https_port':body.local_https_port,'relay_ca':ca.json()['certificate'],'enabled':True}
    private_write(app.settings.state_dir/'remote-config.enc',app.vault.seal(config,'remote-config'))
    return {'bound':True,'url':data['url'],'agent_required':True}

@router.post('/disable')
def disable(request:Request,actor=Depends(authenticate)):
    owner(actor);app=request.app.state;config=read_config(app)
    if config:
        config['enabled']=False;private_write(app.settings.state_dir/'remote-config.enc',app.vault.seal(config,'remote-config'))
    return {'enabled':False}
