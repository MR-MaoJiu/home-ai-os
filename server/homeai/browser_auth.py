import secrets
import time
from urllib.parse import urlsplit
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
import pyotp
from fastapi import APIRouter, Request, Response, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select, delete
from .db import BrowserAccount, Credential, Device, Principal, LoginAttempt, Nonce, now, uid, scope
from .security import Actor, credential
from .crypto import digest
from .data import audit

router=APIRouter(prefix='/api/v1/browser',tags=['网页认证'])
COOKIE='__Host-homeai-session'
CSRF='__Host-homeai-csrf'
ph=PasswordHasher()
DUMMY_HASH=ph.hash(secrets.token_hex(24))


class Setup(BaseModel):
    model_config=ConfigDict(extra='forbid')
    ticket:str=Field(min_length=20,max_length=200)
    username:str=Field(pattern=r'^[a-zA-Z0-9_.@-]{3,80}$')
    password:str=Field(min_length=12,max_length=256)


class Login(BaseModel):
    model_config=ConfigDict(extra='forbid')
    username:str=Field(max_length=80)
    password:str=Field(max_length=256)
    code:str=Field(default='',max_length=8)


class Code(BaseModel):
    code:str=Field(pattern=r'^\d{6}$')


def same_origin(request):
    origin=request.headers.get('origin')
    host=request.headers.get('host','')
    dev_loopback = (request.app.state.settings.environment == 'development' and urlsplit(origin or '').hostname in {'localhost','127.0.0.1'} and request.client and request.client.host in {'127.0.0.1','::1'})
    if not origin or urlsplit(origin).netloc != host or (urlsplit(origin).scheme!='https' and not dev_loopback):
        raise HTTPException(403,'只允许同源 HTTPS 请求')


def cookies(response,token):
    csrf=secrets.token_urlsafe(32)
    response.set_cookie(COOKIE,token,secure=True,httponly=True,samesite='strict',path='/',max_age=28800)
    response.set_cookie(CSRF,csrf,secure=True,httponly=False,samesite='strict',path='/',max_age=28800)
    return csrf


def get_browser_actor(request, setup=False):
    raw=request.cookies.get(COOKIE,'')
    if not raw:raise HTTPException(401,'请登录管理后台')
    if request.method not in {'GET','HEAD','OPTIONS'}:
        same_origin(request)
        a,b=request.cookies.get(CSRF,''),request.headers.get('x-csrf-token','')
        if not a or not secrets.compare_digest(a,b):raise HTTPException(403,'CSRF 校验失败')
    with request.app.state.db() as db:
        session=db.get(Credential,digest(raw.encode()))
        allowed={'browser','browser_setup'} if setup else {'browser'}
        if not session or session.kind not in allowed or session.expires_at<=now():raise HTTPException(401,'网页会话已失效')
        device=db.get(Device,session.device_id)
        user=db.get(Principal,session.user_id)
        if not device or device.revoked or not user:raise HTTPException(401,'网页设备已撤销')
        sensitive = request.method not in {'GET','HEAD','OPTIONS'} and request.url.path.startswith(('/api/v1/secrets','/api/v1/providers','/api/v1/members','/api/v1/devices','/api/v1/remote/disable'))
        sensitive = sensitive or (request.method == 'POST' and request.url.path.startswith('/api/v1/tasks/') and request.url.path.endswith('/reconcile'))
        if sensitive:
            proof=db.get(Nonce,'web-stepup:'+session.digest)
            if not proof or proof.expires_at<=now():raise HTTPException(403,'需要重新验证密码与动态验证码')
        return Actor(user.id,user.household_id,device.id,user.role)


def consume_totp(account,code,vault):
    secret=vault.open(account.totp_secret,account.user_id+':totp')
    counter=int(time.time())//30
    for offset in (0,-1,1):
        candidate=counter+offset
        if candidate>account.last_totp_counter and secrets.compare_digest(pyotp.TOTP(secret).at(candidate*30),code):
            account.last_totp_counter=candidate
            return True
    return False


def limit_key(request,username):
    return digest(((request.client.host if request.client else 'unknown')+':'+username.lower()).encode())


def limit_check(db,key):
    row=db.get(LoginAttempt,key)
    if row and now()-row.window_start<900 and row.failures>=5:raise HTTPException(429,'尝试过多，请 15 分钟后重试')
    return row


def failed(db,key,row):
    if not row:
        row=LoginAttempt(id=key,failures=0,window_start=now());db.add(row)
    if now()-row.window_start>=900:row.failures=0;row.window_start=now()
    row.failures+=1
    db.commit()


@router.post('/setup')
def setup(body:Setup,request:Request,response:Response):
    same_origin(request)
    with request.app.state.db() as db:
        ticket=db.scalar(select(Credential).where(Credential.digest==digest(body.ticket.encode())).with_for_update())
        if not ticket or ticket.kind not in {'browser_bootstrap','browser_recovery'} or ticket.expires_at<=now():raise HTTPException(401,'初始化凭据无效')
        user=db.get(Principal,ticket.user_id)
        if not user or user.role!='infrastructure_owner':raise HTTPException(403,'需要家庭管理员')
        account=db.scalar(select(BrowserAccount).where(BrowserAccount.user_id==user.id))
        if account and ticket.kind!='browser_recovery':raise HTTPException(409,'网页账户已初始化')
        secret=pyotp.random_base32()
        if account:
            db.execute(delete(Credential).where(Credential.user_id==user.id,Credential.kind.in_(['browser','browser_setup'])))
            account.username=body.username.lower();account.password_hash=ph.hash(body.password)
            account.totp_secret=request.app.state.vault.seal(secret,user.id+':totp');account.totp_enabled=False;account.last_totp_counter=-1
        else:
            db.add(BrowserAccount(username=body.username.lower(),user_id=user.id,password_hash=ph.hash(body.password),totp_secret=request.app.state.vault.seal(secret,user.id+':totp')))
        device=Device(id=uid(),user_id=user.id,public_key='',name='网页管理后台')
        db.add(device)
        token=credential(db,user.id,'browser_setup',300,device.id)
        db.delete(ticket)
        db.commit()
        csrf=cookies(response,token)
        return {'status':'TOTP_REQUIRED','secret':secret,'uri':pyotp.TOTP(secret).provisioning_uri(name=body.username,issuer_name='Home AI OS'),'csrf':csrf}


@router.post('/totp/confirm')
def confirm(body:Code,request:Request,response:Response):
    actor=get_browser_actor(request,setup=True)
    with request.app.state.db() as db:
        account=db.scalar(select(BrowserAccount).where(BrowserAccount.user_id==actor.user_id).with_for_update())
        key=limit_key(request,account.username)
        attempt=limit_check(db,key)
        if not consume_totp(account,body.code,request.app.state.vault):
            failed(db,key,attempt)
            raise HTTPException(401,'验证码无效或已使用')
        account.totp_enabled=True
        old=db.get(Credential,digest(request.cookies[COOKIE].encode()))
        db.delete(old)
        token=credential(db,actor.user_id,'browser',28800,actor.device_id)
        db.add(Nonce(id='web-stepup:'+digest(token.encode()),expires_at=now()+300))
        db.commit()
        return {'status':'authenticated','csrf':cookies(response,token)}


@router.post('/login')
def login(body:Login,request:Request,response:Response):
    same_origin(request)
    with request.app.state.db() as db:
        username=body.username.lower()
        key=limit_key(request,username)
        attempt=limit_check(db,key)
        account=db.scalar(select(BrowserAccount).where(BrowserAccount.username==username).with_for_update())
        try:valid=ph.verify(account.password_hash if account else DUMMY_HASH,body.password)
        except VerificationError:valid=False
        if not account or not valid or not account.totp_enabled or not consume_totp(account,body.code,request.app.state.vault):
            failed(db,key,attempt)
            raise HTTPException(401,'登录信息无效')
        device=Device(id=uid(),user_id=account.user_id,public_key='',name='网页会话')
        db.add(device)
        token=credential(db,account.user_id,'browser',28800,device.id)
        db.add(Nonce(id='web-stepup:'+digest(token.encode()),expires_at=now()+300))
        if attempt:db.delete(attempt)
        db.commit()
        return {'status':'authenticated','csrf':cookies(response,token)}


@router.post('/logout')
def logout(request:Request,response:Response):
    actor=get_browser_actor(request)
    with request.app.state.db() as db:
        session=db.get(Credential,digest(request.cookies[COOKIE].encode()))
        db.delete(session)
        db.get(Device,actor.device_id).revoked=True
        db.commit()
    response.delete_cookie(COOKIE,path='/',secure=True,httponly=True,samesite='strict')
    response.delete_cookie(CSRF,path='/',secure=True,samesite='strict')
    return {'signed_out':True}

class Reauthenticate(BaseModel):
    password:str=Field(max_length=256)
    code:str=Field(pattern=r'^\d{6}$')

@router.post('/reauth')
def reauthenticate(body:Reauthenticate,request:Request):
    actor=get_browser_actor(request)
    with request.app.state.db() as db:
        account=db.scalar(select(BrowserAccount).where(BrowserAccount.user_id==actor.user_id).with_for_update())
        key=limit_key(request,account.username);attempt=limit_check(db,key)
        try:valid=ph.verify(account.password_hash,body.password)
        except VerificationError:valid=False
        if not valid or not consume_totp(account,body.code,request.app.state.vault):
            failed(db,key,attempt);raise HTTPException(401,'重新验证失败')
        identifier='web-stepup:'+digest(request.cookies[COOKIE].encode())
        proof=db.get(Nonce,identifier)
        if proof:proof.expires_at=now()+300
        else:db.add(Nonce(id=identifier,expires_at=now()+300))
        db.commit();return {'verified_for_seconds':300}
