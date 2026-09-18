"""家庭端申请官方或兼容平台服务，客服确认后自动完成绑定。"""
import base64,json,secrets,time
import httpx
from fastapi import APIRouter,Depends,Request,HTTPException
from pydantic import BaseModel,ConfigDict,Field
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import ec
from .security import authenticate,owner
from .server_identity import identity
from .connect_signalling import DEFAULT_PORTAL,platform_origin,inspect_platform
from .remote import binding_scope,read_config,log_change
from .private_files import private_write
router=APIRouter(prefix='/api/v1/remote')

class Platform(BaseModel):
    model_config=ConfigDict(extra='forbid')
    portal_url:str=Field(default=DEFAULT_PORTAL,max_length=300)
class Apply(Platform):
    plan_id:str=Field(min_length=1,max_length=100)

async def public_request(portal,path,body=None):
    async with httpx.AsyncClient(timeout=12,trust_env=False,follow_redirects=False) as client:
        async with client.stream('POST' if body is not None else 'GET',platform_origin(portal)+path,json=body) as r:
            r.raise_for_status();raw=bytearray()
            async for chunk in r.aiter_bytes():
                raw.extend(chunk)
                if len(raw)>65536:raise ValueError('平台响应过大')
            return json.loads(raw)

@router.post('/catalog')
async def catalog(body:Platform,actor=Depends(authenticate)):
    owner(actor)
    try:
        await inspect_platform(body.portal_url)
        value=await public_request(body.portal_url,'/api/public/catalog')
        if value.get('onboarding_version')!=1 or not isinstance(value.get('plans'),list):raise ValueError()
        return value
    except (ValueError,httpx.HTTPError):raise HTTPException(502,'平台不支持客服申请流程或暂时不可用') from None

@router.post('/applications')
async def apply(body:Apply,request:Request,actor=Depends(authenticate)):
    owner(actor);app=request.app.state;binding_scope(read_config(app),actor)
    if read_config(app) and read_config(app).get('enabled'):raise HTTPException(409,'请先停用当前绑定，再申请新服务')
    available=await catalog(body,actor)
    if not any(p.get('id')==body.plan_id for p in available['plans']):raise HTTPException(409,'套餐未开放')
    support=available.get('support',{})
    if not any(support.get(k) for k in ('wechat','email','qq')):raise HTTPException(409,'平台尚未配置客服联系方式')
    server,key=identity(app);identifier=secrets.token_hex(16)
    payload={'version':1,'id':identifier,'server_id':server,'public_key':key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode(),'plan_id':body.plan_id,'expires':int(time.time())+86400}
    raw=json.dumps(payload,separators=(',',':'),sort_keys=True).encode()
    code=base64.b64encode(json.dumps({'payload':base64.b64encode(raw).decode(),'signature':base64.b64encode(key.sign(b'homeai-service-application:v1\n'+raw,ec.ECDSA(hashes.SHA256()))).decode()}).encode()).decode()
    value={'household_id':actor.household_id,'portal_url':platform_origin(body.portal_url),'code':code,'id':identifier,'expires':payload['expires']}
    private_write(app.settings.state_dir/'service-application.enc',app.vault.seal(value,'service-application'))
    log_change(app,actor,'remote.application_created')
    return {'request_code':code,'expires':payload['expires'],'support':support}

@router.get('/application')
def application_status(request:Request,actor=Depends(authenticate)):
    owner(actor);app=request.app.state;path=app.settings.state_dir/'service-application.enc'
    if not path.exists():return {'status':'NONE'}
    value=app.vault.open(path.read_text(),'service-application');binding_scope(value,actor)
    return {'status':value.get('status') or ('EXPIRED' if value['expires']<=time.time() else 'WAITING'),'request_code':value['code'],'portal_url':value['portal_url'],'expires':value['expires']}

async def poll_application(app):
    path=app.settings.state_dir/'service-application.enc'
    if not path.exists():return
    value=app.vault.open(path.read_text(),'service-application')
    if value['expires']<=time.time() or value.get('status')=='APPROVED':return
    timestamp=int(time.time());nonce=secrets.token_hex(16);_,key=identity(app)
    proof='homeai-service-poll:v1\n'+value['id']+'\n'+str(timestamp)+'\n'+nonce
    result=await public_request(value['portal_url'],'/api/public/applications/status',{'code':value['code'],'timestamp':timestamp,'nonce':nonce,'signature':base64.b64encode(key.sign(proof.encode(),ec.ECDSA(hashes.SHA256()))).decode()})
    if result.get('status')!='APPROVED':return
    capabilities=await inspect_platform(value['portal_url'])
    current=app.vault.open(path.read_text(),'service-application')
    if current['id']!=value['id']:return
    config={**capabilities,'household_id':value['household_id'],'instance_id':result['instance_id'],'credential':result['credential'],'enabled':True}
    # 开通服务即采用该平台下发的网络配置，不要求用户再选来源。
    network={'mode':'platform','stun_urls':[],'household_id':value['household_id']}
    private_write(app.settings.state_dir/'remote-network.enc',app.vault.seal(network,'remote-network'))
    private_write(app.settings.state_dir/'remote-config.enc',app.vault.seal(config,'remote-config'))
    value['status']='APPROVED';private_write(path,app.vault.seal(value,'service-application'))
