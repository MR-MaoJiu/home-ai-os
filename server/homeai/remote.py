"""可选远程连接客户端接口，不影响 Core 的本地运行。"""
import json
from fastapi import APIRouter,Request,Depends
from .security import authenticate,owner
from .private_files import private_write

router=APIRouter(prefix='/api/v1/remote',tags=['可选远程连接'])

def log_change(app, actor, action):
    from .data import audit
    from .db import scope
    with app.db() as db:
        scope(db,actor.user_id,actor.household_id)
        audit(db,actor,action,'remote')
        db.commit()


def binding_scope(config, actor):
    if config and config.get('household_id') and config['household_id'] != actor.household_id:
        raise HTTPException(403, '连接配置属于另一家庭')


def read_config(app):
    path=app.settings.state_dir/'remote-config.enc'
    return app.vault.open(path.read_text(),'remote-config') if path.exists() else None

@router.get('/status')
def status(request:Request,actor=Depends(authenticate)):
    owner(actor);config=read_config(request.app.state)
    binding_scope(config,actor)
    import time
    direct = bool(config and config.get('transport_policy') == 'direct_only' and config.get('protocol') == 1)
    current_file = request.app.state.settings.state_dir/'direct-status.json'
    current = json.loads(current_file.read_text()) if current_file.exists() else {'state':'not_running'}
    if current.get('checked_at') and time.time()-current['checked_at']>10:current['state']='stale'
    path=request.app.state.settings.state_dir/'remote-network.enc'
    prefs=request.app.state.vault.open(path.read_text(),'remote-network') if path.exists() else {'mode':'platform','stun_urls':[]}
    binding_scope(prefs,actor)
    return {'network':prefs,'portal_url':config.get('portal_url') if config else None,'configured':config is not None,'enabled':bool(direct and config.get('enabled')),'previously_enabled':bool(config and not direct and config.get('enabled')),
            'transport_policy':'direct_only','direct_ready':bool(direct and request.app.state.settings.direct_enabled),'relay_allowed':False,
            'migration_required':bool(config and not direct),'runtime':current if direct else {'state':'direct_not_ready'}}

@router.post('/disable')
def disable(request:Request,actor=Depends(authenticate)):
    owner(actor);app=request.app.state;config=read_config(app)
    binding_scope(config,actor)
    if config:
        config['enabled']=False;private_write(app.settings.state_dir/'remote-config.enc',app.vault.seal(config,'remote-config'))
        log_change(app,actor,'remote.disable')
    return {'enabled':False}


from pydantic import BaseModel, ConfigDict, Field
from fastapi import HTTPException


class PlatformCheck(BaseModel):
    model_config = ConfigDict(extra='forbid')
    portal_url: str = Field(default='https://homeai-connect.pintheworld.cn', max_length=300)


@router.post('/platform-check')
async def platform_check(body: PlatformCheck, actor=Depends(authenticate)):
    import httpx
    from .connect_signalling import inspect_platform
    owner(actor)
    try:
        return await inspect_platform(body.portal_url)
    except ValueError:
        raise HTTPException(422, '平台地址或纯直连协议不兼容') from None
    except httpx.HTTPError:
        raise HTTPException(502, '无法验证平台，请检查地址、证书与网络') from None


@router.post('/migrate')
async def migrate(request: Request, actor=Depends(authenticate)):
    from .connect_signalling import Broker, validate_capabilities
    owner(actor)
    app = request.app.state
    old = read_config(app)
    if not old:
        raise HTTPException(409, '没有可迁移的绑定')
    if old.get('household_id') and old['household_id'] != actor.household_id:
        raise HTTPException(403, '连接配置属于另一家庭')
    config = {key: old[key] for key in ('portal_url', 'instance_id', 'credential')}
    config.update(protocol=1, transport_policy='direct_only', household_id=actor.household_id, enabled=True)
    await Broker(app, config).request('GET', '/api/direct/offers')
    capabilities = await Broker(app, config).request('GET', '/api/direct/capabilities')
    config['stun_urls'] = validate_capabilities(capabilities)['stun_urls']
    # 保留原密文用于人工恢复；新的活动配置不再持有中继地址、CA 或端口。
    archive = app.settings.state_dir/'remote-legacy.enc'
    if not archive.exists():
        private_write(archive, app.vault.seal(old, 'remote-legacy'))
    private_write(app.settings.state_dir/'remote-config.enc', app.vault.seal(config, 'remote-config'))
    log_change(app,actor,'remote.migrate')
    return {'migrated': True, 'transport_policy': 'direct_only', 'runtime_enabled': app.settings.direct_enabled}


@router.post('/direct-access')
async def direct_access(request: Request, actor=Depends(authenticate)):
    from .db import Device
    from .connect_signalling import Broker, validate_stun_urls, effective_stun
    if not request.headers.get('authorization', '').startswith('Bearer '):
        raise HTTPException(403, '需要已配对设备的签名请求')
    app = request.app.state
    config = read_config(app)
    if not config or not config.get('enabled') or config.get('household_id') != actor.household_id:
        raise HTTPException(409, '家庭尚未启用纯直连平台绑定')
    with app.db() as db:
        device = db.get(Device, actor.device_id)
        grant = await Broker(app, config).request('POST', '/api/direct/grants', {'device_id': device.id, 'public_key': device.public_key})
    log_change(app,actor,'remote.device_authorize')
    return {**grant, 'portal_url': config['portal_url'], 'instance_id': config['instance_id'],
            'stun_urls': effective_stun(app,config), 'transport_policy': 'direct_only'}


@router.post('/resume')
async def resume(request: Request, actor=Depends(authenticate)):
    from .connect_signalling import Broker, validate_stun_urls, effective_stun
    owner(actor)
    app = request.app.state
    config = read_config(app)
    if not config or config.get('household_id') != actor.household_id or config.get('transport_policy') != 'direct_only':
        raise HTTPException(409, '需要有效的纯直连绑定')
    await Broker(app, config).request('GET', '/api/direct/offers')
    config['enabled'] = True
    private_write(app.settings.state_dir/'remote-config.enc', app.vault.seal(config, 'remote-config'))
    log_change(app,actor,'remote.resume')
    return {'enabled': True, 'transport_policy': 'direct_only'}


@router.post('/refresh-node')
async def refresh_node(request: Request, actor=Depends(authenticate)):
    from .connect_signalling import Broker, validate_capabilities
    owner(actor)
    app=request.app.state;config=read_config(app)
    if not config or config.get('household_id')!=actor.household_id:
        raise HTTPException(409,'没有本家庭的直连绑定')
    value=await Broker(app,config).request('GET','/api/direct/capabilities')
    config['stun_urls']=validate_capabilities(value)['stun_urls']
    private_write(app.settings.state_dir/'remote-config.enc',app.vault.seal(config,'remote-config'))
    log_change(app,actor,'remote.node_refresh')
    return {'updated':True,'requires_device_authorization_refresh':True}


class NetworkInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    mode:str=Field(pattern='^(platform|custom)$')
    stun_urls:list[str]=Field(default_factory=list,max_length=2)

@router.put('/stun')
def configure_stun(body:NetworkInput,request:Request,actor=Depends(authenticate)):
    from .connect_signalling import validate_stun_urls
    owner(actor);app=request.app.state;binding_scope(read_config(app),actor)
    path=app.settings.state_dir/'remote-network.enc'
    if path.exists():binding_scope(app.vault.open(path.read_text(),'remote-network'),actor)
    try:urls=validate_stun_urls(body.stun_urls)
    except ValueError:raise HTTPException(422,'只接受 STUN 地址，禁止 TURN') from None
    if body.mode=='custom' and not urls:raise HTTPException(422,'自定义模式需要至少一个 STUN 地址')
    value={'mode':body.mode,'stun_urls':urls if body.mode=='custom' else [],'household_id':actor.household_id}
    private_write(app.settings.state_dir/'remote-network.enc',app.vault.seal(value,'remote-network'))
    log_change(app,actor,'remote.network_updated')
    return {'saved':True,'requires_new_pairing':True}
