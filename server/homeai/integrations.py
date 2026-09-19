"""MCP 目录核对；只读发现不会自动获得工具调用权限。"""
import asyncio
from fastapi import APIRouter,Request,Depends,HTTPException
from pydantic import BaseModel,Field,ConfigDict
from .security import authenticate,owner
from .contracts import ProviderManifest
from .policy import CAPABILITIES
from .db import Secret
import httpx

router=APIRouter(prefix='/api/v1/integrations',tags=['能力接入'])
class Inspect(BaseModel):
    model_config=ConfigDict(extra='forbid')
    manifest:ProviderManifest

@router.post('/mcp/inspect')
async def inspect(body:Inspect,request:Request,actor=Depends(authenticate)):
    owner(actor)
    manifest=body.manifest
    from urllib.parse import urlparse
    if manifest.adapter!='mcp' or urlparse(manifest.endpoint).hostname not in manifest.allowed_hosts:raise HTTPException(422,'MCP 地址与网络授权不一致')
    headers={}
    if manifest.secret_id:
        with request.app.state.db() as db:
            secret=db.get(Secret,manifest.secret_id)
            if not secret or secret.owner_id!=actor.user_id or secret.provider_id!=manifest.id:raise HTTPException(403,'凭据不属于当前接入')
            headers['Authorization']='Bearer '+request.app.state.vault.open(secret.value,actor.user_id+':secret:'+secret.id)
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from homeai_providers.mcp_contract import catalog
    try:
        async with asyncio.timeout(20),httpx.AsyncClient(headers=headers,timeout=10,follow_redirects=False,trust_env=False) as client:
            async with streamable_http_client(manifest.endpoint,http_client=client) as streams:
                async with ClientSession(streams[0],streams[1]) as session:
                    await session.initialize();fingerprint,tools=await catalog(session)
                    return {'catalog_sha256':fingerprint,'tools':tools,'capabilities':list(CAPABILITIES)}
    except Exception:raise HTTPException(502,'MCP 目录读取失败，请核对地址、服务及凭据') from None


class SkillInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    content:str=Field(min_length=1,max_length=16000)
    enabled:bool=False


def parse_skill(content):
    import yaml
    if not content.startswith('---\n'):raise HTTPException(422,'SKILL.md 必须包含 name、description 的 YAML 头部')
    parts=content.split('---',2)
    try:metadata=yaml.safe_load(parts[1])
    except Exception:raise HTTPException(422,'Skill 头部格式错误') from None
    if len(parts)!=3 or not isinstance(metadata,dict):raise HTTPException(422,'Skill 头部格式错误')
    name=metadata.get('name');description=metadata.get('description')
    if not isinstance(name,str) or not 1<=len(name)<=100 or not isinstance(description,str) or not 1<=len(description)<=1000:raise HTTPException(422,'Skill 需要有效名称和描述')
    if not parts[2].strip():raise HTTPException(422,'Skill 正文为空')
    return name,description

@router.get('/skills')
def skills(request:Request,actor=Depends(authenticate)):
    from sqlalchemy import select
    from .db import AgentSkill,scope
    owner(actor)
    with request.app.state.db() as db:
        scope(db,actor.user_id,actor.household_id)
        return [{'id':r.id,'name':r.name,'description':r.description,'enabled':r.enabled,'mine':r.owner_id==actor.user_id} for r in db.scalars(select(AgentSkill).where(AgentSkill.household_id==actor.household_id))]

@router.post('/skills')
def add_skill(body:SkillInput,request:Request,actor=Depends(authenticate)):
    from .db import AgentSkill,scope,uid
    from .data import audit
    owner(actor);name,description=parse_skill(body.content)
    with request.app.state.db() as db:
        scope(db,actor.user_id,actor.household_id)
        identifier=uid()
        db.add(AgentSkill(id=identifier,owner_id=actor.user_id,household_id=actor.household_id,name=name,description=description,content=request.app.state.vault.seal(body.content,actor.user_id+':skill:'+identifier),enabled=body.enabled))
        audit(db,actor,'skill.install',identifier);db.commit()
        return {'id':identifier}

@router.post('/skills/{identifier}/{action}')
def manage_skill(identifier:str,action:str,request:Request,actor=Depends(authenticate)):
    from .db import AgentSkill
    from .security import own
    from .data import audit
    owner(actor)
    with request.app.state.db() as db:
        row=own(db,AgentSkill,identifier,actor)
        if action=='delete':db.delete(row)
        elif action in ('enable','disable'):row.enabled=action=='enable'
        else:raise HTTPException(422,'不支持的 Skill 操作')
        audit(db,actor,'skill.'+action,identifier);db.commit()
        return {'updated':True}


def skill_context(app,db,actor):
    from sqlalchemy import select
    from .db import AgentSkill
    items=[];size=0
    for row in db.scalars(select(AgentSkill).where(AgentSkill.household_id==actor.household_id,AgentSkill.enabled.is_(True)).order_by(AgentSkill.id)):
        value=app.vault.open(row.content,row.owner_id+':skill:'+row.id)
        size+=len(value.encode())
        if size>24000:raise HTTPException(409,'启用 Skill 内容超过上下文预算，请停用不需要的 Skill')
        items.append(value)
    return '\n\n'.join(items)
