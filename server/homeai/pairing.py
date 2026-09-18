"""同一张短期二维码完成局域网或外网配对。平台只见加密引导消息。"""
import base64,hashlib,json,os,secrets
from fastapi import APIRouter,Depends,Request,Response,HTTPException
from sqlalchemy import select,delete
from .db import Credential,Device,Principal,PairEnrollment,now,uid,scope
from .security import authenticate,owner,credential,verify
from .crypto import digest
from .contracts import PairRequest
from .data import audit
from .server_identity import public_identity,addresses
from .remote import read_config,binding_scope
from .connect_signalling import Broker,effective_stun
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
router=APIRouter(prefix='/api/v1')


def crypt(token,identifier,value,direction,decrypt=False):
    cipher=AESGCM(hashlib.sha256(('homeai-pair-bootstrap:v1\n'+token).encode()).digest())
    aad=('homeai-pair-bootstrap:v1\n'+identifier+'\n'+direction).encode()
    if decrypt:
        raw=base64.b64decode(value,validate=True)
        return json.loads(cipher.decrypt(raw[:12],raw[12:],aad))
    nonce=os.urandom(12)
    return base64.b64encode(nonce+cipher.encrypt(nonce,json.dumps(value,separators=(',',':')).encode(),aad)).decode()


def consume(app,db,body,household_id=None):
    verify(body.public_key,body.signature,('homeai-pair:'+body.token).encode())
    invitation=db.scalar(select(Credential).where(Credential.digest==digest(body.token.encode())).with_for_update())
    if not invitation or invitation.kind!='pair' or invitation.expires_at<=now():raise HTTPException(401,'配对码失效')
    user=db.get(Principal,invitation.user_id)
    if not user or household_id is not None and user.household_id!=household_id:raise HTTPException(403,'配对不属于当前家庭')
    device=Device(id=uid(),user_id=user.id,public_key=body.public_key,name=body.name);db.add(device)
    result={'access_token':credential(db,user.id,'access',app.settings.session_seconds,device.id),'refresh_token':credential(db,user.id,'refresh',30*86400,device.id),'device_id':device.id,'expires_in':app.settings.session_seconds}
    db.delete(invitation)
    return result


@router.post('/members/{user_id}/pairing')
async def issue_pairing(user_id:str,request:Request,response:Response,actor=Depends(authenticate)):
    owner(actor);app=request.app.state;config=read_config(app);binding_scope(config,actor)
    with app.db() as db:
        user=db.get(Principal,user_id)
        if not user or user.household_id!=actor.household_id:raise HTTPException(404,'成员不存在')
    public=public_identity(app)
    remote=bool(config and config.get('enabled') and config.get('protocol')==1)
    urls=public['addresses']
    if not urls and not remote:
        raise HTTPException(409,'请先在部署配置 HOMEAI_SERVER_ADDRESSES 中设置手机可访问的家庭 HTTPS 地址')
    identifier=secrets.token_hex(16);ticket=None
    if remote:
        ticket=await Broker(app,config).request('POST','/api/direct/enrollments',{'id':identifier})
    with app.db() as db:
        db.execute(delete(PairEnrollment).where(PairEnrollment.expires<=now()))
        token=credential(db,user_id,'pair',300)
        expires=min(now()+300,ticket['expires']) if ticket else now()+300
        if remote:
            stored={'token':token,'instance_id':config['instance_id'],'portal_url':config['portal_url']}
            db.add(PairEnrollment(id=identifier,household_id=actor.household_id,user_id=user_id,expires=expires,ciphertext=app.vault.seal(stored,'enrollment:'+identifier)))
        scope(db,actor.user_id,actor.household_id)
        audit(db,actor,'member.pairing_issued',user_id);db.commit()
    code={'schema_version':'3.0' if remote else '2.0','url':urls[0] if urls else '',**public,'token':token,'expires_at':expires}
    if remote:code['remote']={**ticket,'portal_url':config['portal_url'],'instance_id':config['instance_id']}
    response.headers['Cache-Control']='no-store'
    return {'code':code,'expires_at':expires,'mode':'remote' if remote else 'lan'}


async def process_enrollment(app,config,item):
    identifier=item['id']
    with app.db() as db:
        row=db.scalar(select(PairEnrollment).where(PairEnrollment.id==identifier).with_for_update())
        if not row or row.expires<=now() or row.household_id!=config['household_id']:raise ValueError('配对授权不存在')
        value=app.vault.open(row.ciphertext,'enrollment:'+identifier)
        if value['instance_id']!=config['instance_id'] or value['portal_url']!=config['portal_url']:raise ValueError('配对平台已变化')
        request_hash=digest(item['ciphertext'].encode())
        if value.get('request_hash') and value['request_hash']!=request_hash:raise ValueError('配对请求不能替换')
        if not value.get('tokens'):
            body=PairRequest.model_validate(crypt(value['token'],identifier,item['ciphertext'],'request',True))
            if body.token!=value['token']:raise ValueError('配对凭据不匹配')
            value.update(request_hash=request_hash,tokens=consume(app,db,body,row.household_id),public_key=body.public_key)
            # 设备、凭据及加密恢复结果在同一个事务写入，进程中断不会重复创建设备。
            row.ciphertext=app.vault.seal(value,'enrollment:'+identifier);db.commit()
        device=db.get(Device,value['tokens']['device_id'])
        if not device or device.revoked:raise ValueError('设备已撤销')
        if value.get('response'):return value['response']
        grant=await Broker(app,config).request('POST','/api/direct/grants',{'device_id':device.id,'public_key':value['public_key']})
        result={**value['tokens'],'direct_access':{**grant,'portal_url':config['portal_url'],'instance_id':config['instance_id'],'stun_urls':effective_stun(app,config),'transport_policy':'direct_only'}}
        encrypted=crypt(value['token'],identifier,result,'response')
        value['response']=encrypted
        row.ciphertext=app.vault.seal(value,'enrollment:'+identifier);db.commit();return encrypted


@router.get('/pairing/address')
def pairing_address(request:Request,actor=Depends(authenticate)):
    owner(actor)
    import ipaddress,ifaddr
    candidates=[]
    networks=[ipaddress.ip_network(v) for v in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')]
    for adapter in ifaddr.get_adapters():
        for value in adapter.ips:
            if not isinstance(value.ip,str):continue
            ip=ipaddress.ip_address(value.ip)
            if any(ip in network for network in networks):
                candidate=f'https://{ip}:{request.url.port or 443}'
                if candidate not in candidates:candidates.append(candidate)
    from .server_identity import configured_addresses
    return {'addresses':configured_addresses(request.app.state),'candidates':candidates}

from pydantic import BaseModel,ConfigDict,Field
class AddressInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    url:str=Field(max_length=300)

@router.put('/pairing/address')
def save_address(body:AddressInput,request:Request,actor=Depends(authenticate)):
    owner(actor)
    from .private_files import private_write
    from urllib.parse import urlparse
    import ipaddress
    try:
        url=addresses([body.url])[0]
    except ValueError:
        raise HTTPException(422,'需要有效的家庭 HTTPS 根地址') from None
    host=urlparse(url).hostname
    if host=='localhost':raise HTTPException(422,'不能使用本机回环地址')
    try:local=ipaddress.ip_address(host).is_loopback
    except ValueError:local=False
    if local:raise HTTPException(422,'不能使用本机回环地址')
    app=request.app.state
    binding_scope(read_config(app),actor)
    path=app.settings.state_dir/'pairing-address.enc'
    if path.exists():binding_scope(app.vault.open(path.read_text(),'pairing-address'),actor)
    private_write(path,app.vault.seal({'addresses':[url],'household_id':actor.household_id},'pairing-address'))
    with app.db() as db:
        scope(db,actor.user_id,actor.household_id)
        audit(db,actor,'pairing.address_updated','server');db.commit()
    return {'addresses':[url]}
