"""服务器观测由 Core 固定实现；性能查询自动继续，截屏须逐次确认。"""
import platform
from typing import Literal
from fastapi import HTTPException
from pydantic import Field, model_validator
from .contracts import Contract
from .crypto import canonical, digest
from .db import Device, Principal, now
from .resource_tools import tool


class HostInspectionRequest(Contract):
    targets: list[Literal['cpu','memory','screenshot']] = Field(min_length=1,max_length=3)
    purpose: str = Field(min_length=1,max_length=1000)

    @model_validator(mode='after')
    def unique(self):
        if len(set(self.targets)) != len(self.targets) or not self.purpose.strip():
            raise ValueError('读取项目不能重复且需要说明用途')
        self.targets=sorted(self.targets)
        return self


TOOL = tool('inspect_server','读取家庭服务器当前 CPU、内存或主显示器截屏。必须实际调用此工具；CPU和内存查询直接执行，不询问用户是否继续。包含截屏时由服务器展示用途、读取内容和影响，获得本次同意后才采集，不能口头声称已申请或已读到。性能指标仅代表服务进程可见的系统视图。截屏须有本机授权及操作系统屏幕权限，图片交给本人，不读取其他成员的私有数据库。',
    {'targets':{'type':'array','items':{'type':'string','enum':['cpu','memory','screenshot']},'minItems':1,'maxItems':3},
     'purpose':{'type':'string','description':'向用户说明本次读取的用途，不包含虚构执行结果。'}},['targets','purpose'])


def allowed_targets(app,actor):
    if actor.role not in {'adult','infrastructure_owner'} or platform.system() not in {'Darwin','Linux'}:
        return []
    targets=['cpu','memory']
    from .shell_configuration import load_policy
    try:
        policy=load_policy(app.settings)
        if platform.system()=='Darwin' and policy.host_enabled and actor.user_id in policy.host_members:
            targets.append('screenshot')
    except HTTPException:
        pass
    return targets


def prepare(app,db,actor,body,args):
    if body.get('_automation_id') or body.get('_automation_scope')=='family':
        raise HTTPException(403,'服务器观测需要本人成员逐次确认，不能从自动化静默采集')
    try:request=HostInspectionRequest.model_validate(args)
    except ValueError:raise HTTPException(422,'服务器观测参数无效') from None
    denied = set(request.targets) - set(allowed_targets(app,actor))
    if denied:
        labels = {'cpu':'CPU使用情况','memory':'内存统计','screenshot':'主显示器截屏'}
        reason = '当前成员或服务器未获' + '、'.join(labels[target] for target in request.targets if target in denied) + '的读取权限'
        if 'screenshot' in denied:
            reason += '；截屏需本机管理员明确授权此成员'
        raise HTTPException(403,reason)
    system=platform.system()
    policy_hash=None
    if 'screenshot' in request.targets:
        from .shell_configuration import load_policy
        policy_hash=digest(canonical(load_policy(app.settings).model_dump()))
    commands=[]
    if 'cpu' in request.targets:
        commands.append('/usr/bin/top -l 2 -s 1 -n 0' if system=='Darwin' else '读取 /proc/stat 的两次计数（间隔 0.25 秒）')
    if 'memory' in request.targets:
        commands.extend(['/usr/sbin/sysctl -n hw.memsize','/usr/bin/vm_stat'] if system=='Darwin' else ['读取 /proc/meminfo'])
    if 'screenshot' in request.targets:commands.append('/usr/sbin/screencapture -x -m -t png <本任务临时目录>/screen.png')
    summary='读取一次当前服务器的'+ '、'.join({'cpu':'CPU使用情况','memory':'内存统计','screenshot':'主显示器画面'}[item] for item in request.targets)+'，结果仅返回发起本任务的成员。'
    approval={'version':1,'source':'server_policy','execution_location':'家庭服务器（服务进程可见系统）',
        'execution_identity':'家庭服务进程的系统账号，不自动提权','command':'\n'.join(commands),
        'constraints':['仅执行服务端固定采集程序，不接受任意命令或文件路径','只采集本次确认的项目，取消后停止后续处理'],
        'resource_scope':{'targets':request.targets},'effects':{'summary':summary,
        'file_changes':['生成加密截图附件；处理后清理临时明文文件'] if 'screenshot' in request.targets else [],
        'service_changes':[], 'uncertainties':['截屏可能包含私人画面；手机同意不替代服务器系统的屏幕录制权限'] if 'screenshot' in request.targets else []},
        'proposal_impact':None}
    return {**request.model_dump(),'execution':{'platform':system,'policy_fingerprint':policy_hash},'approval_context':approval}


async def execute(app,db,actor,task,body,prepared,invocation_id):
    original={k:v for k,v in prepared.items() if k not in {'execution','approval_context'}}
    if prepare(app,db,actor,body,original)!=prepared:
        raise HTTPException(409,'服务器观测范围或权限已变化，请重新审批')
    def check():
        db.expire_all()
        db.refresh(task)
        device=db.get(Device,actor.device_id)
        member=db.get(Principal,actor.user_id)
        if task.cancel_requested or task.deadline<=now() or not device or device.revoked or not member or member.role!=actor.role:
            raise HTTPException(409,'任务、设备或成员权限已失效')
        if prepare(app,db,actor,body,original)!=prepared:
            raise HTTPException(409,'服务器观测权限已撤回')
    check()
    from .host_inspection import collect_metrics,capture_screen
    metrics=[item for item in prepared['targets'] if item!='screenshot']
    result={'status':'observed','observations':await collect_metrics(metrics) if metrics else {},'parts':[]}
    check()
    if 'screenshot' in prepared['targets']:
        raw=await capture_screen()
        check()
        from .resource_assets import store_asset
        asset=store_asset(app,db,actor,task,invocation_id+':screenshot',raw,'服务器截屏.png','image',{'origin':'server_capture','created_at':now()})
        result['parts']=asset['parts']
        result['note']='主显示器截屏已作为私有附件提供。此回执不包含图像理解结果，不能猜测画面；若需要分析图片，需按图片权限流程单独处理。'
    result['text']='已读取本次获准的服务器信息。'
    return result
