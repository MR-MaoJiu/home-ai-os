"""固定内置目录。网页只提交选择，不接受命令、下载地址或宿主路径。"""
from fastapi import APIRouter,Depends,Request,HTTPException
from pydantic import BaseModel,ConfigDict
from .db import BuiltinDeployment,Provider,now,scope
from .security import authenticate,owner
from .data import audit

MODELS={
 'qwen3-0.6b':{'name':'Qwen3 0.6B · 轻量（工具规划能力有限）','repo':'Qwen/Qwen3-0.6B-GGUF','revision':'23749fefcc72300e3a2ad315e1317431b06b590a','file':'Qwen3-0.6B-Q8_0.gguf','bytes':639446688,'sha256':'9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031'},
 'qwen3-4b':{'name':'Qwen3 4B · 日常对话与工具规划','repo':'Qwen/Qwen3-4B-GGUF','revision':'bc640142c66e1fdd12af0bd68f40445458f3869b','file':'Qwen3-4B-Q4_K_M.gguf','bytes':2497280256,'sha256':'7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5'},
}
router=APIRouter(prefix='/api/v1/builtins',tags=['内置服务'])
class Selection(BaseModel):
    model_config=ConfigDict(extra='forbid')
    variant:str
    enabled:bool=True

@router.get('')
def catalog(request:Request,actor=Depends(authenticate)):
    owner(actor)
    with request.app.state.db() as db:
        result=[]
        for identifier,name,variants in [('local-model','本地模型',[{'id':key,**value} for key,value in MODELS.items()]),('web-search','联网搜索',[{'id':'searxng','name':'SearXNG'}])]:
            row=db.get(BuiltinDeployment,identifier)
            result.append({'id':identifier,'name':name,'variants':variants,'variant':row.variant if row else variants[-1]['id'],'enabled':row.enabled if row else False,'status':row.status if row else 'not_installed','error':row.error if row else None})
        marker=request.app.state.settings.state_dir/'builtin-worker.heartbeat'
        return {'items':result,'worker_online':marker.exists() and now()-marker.stat().st_mtime<30}

@router.put('/{identifier}',status_code=202)
def configure(identifier:str,body:Selection,request:Request,actor=Depends(authenticate)):
    owner(actor)
    if identifier not in ('local-model','web-search') or body.variant not in (MODELS if identifier=='local-model' else {'searxng'}):raise HTTPException(422,'无效内置服务或模型版本')
    if request.app.state.settings.environment=='production':raise HTTPException(409,'生产隔离未验收，不能从开发部署器启用')
    with request.app.state.db() as db:
        row=db.get(BuiltinDeployment,identifier)
        if not row:row=BuiltinDeployment(id=identifier,variant=body.variant);db.add(row)
        row.variant=body.variant;row.enabled=body.enabled;row.status='queued';row.error=None;row.updated_at=now()
        provider=db.get(Provider,'builtin.'+identifier)
        if provider:provider.enabled=False
        scope(db,actor.user_id,actor.household_id);audit(db,actor,'builtin.configure',identifier,body.model_dump())
        db.commit()
        return {'status':'queued'}
