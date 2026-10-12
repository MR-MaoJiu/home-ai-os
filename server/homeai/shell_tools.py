"""隔离计算自动继续，宿主管理逐次审批；脚本和输出不成为权限来源。"""
import asyncio
import os
import shlex
from pathlib import PurePosixPath
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, StrictInt, field_validator, model_validator
from .contracts import Contract
from .crypto import canonical, digest
from .db import Device, Principal, Task, now, scope
from .security import authenticate, owner
from .resource_tools import tool

router = APIRouter(prefix='/api/v1/manage/shell', tags=['服务端 Shell'])


def relative_path(value):
    path = PurePosixPath(value)
    if not value or len(value) > 160 or path.is_absolute() or any(part in {'', '.', '..'} for part in value.split('/')) or any(ord(c) < 32 for c in value) or '\\' in value:
        raise ValueError('文件路径必须是工作区内的相对路径')
    return value


class InputFile(Contract):
    record_id: str = Field(min_length=1, max_length=100)
    filename: str
    _path = field_validator('filename')(relative_path)


class OutputFile(Contract):
    path: str
    kind: Literal['image', 'video', 'file'] = 'file'
    _path = field_validator('path')(relative_path)


class ShellRequest(Contract):
    language: Literal['shell', 'python'] = 'shell'
    script: str = Field(min_length=1, max_length=20000)
    impact_summary: str = Field(default='', max_length=2000)
    inputs: list[InputFile] = Field(default_factory=list, max_length=8)
    outputs: list[OutputFile] = Field(default_factory=list, max_length=8)
    timeout_seconds: StrictInt = Field(default=120, ge=1, le=300)

    @model_validator(mode='after')
    def valid(self):
        if not self.script.strip() or '\x00' in self.script or '\x00' in self.impact_summary:
            raise ValueError('脚本不能为空或包含空字节')
        for paths in ([item.filename for item in self.inputs], [item.path for item in self.outputs]):
            if len(paths) != len(set(paths)):
                raise ValueError('文件路径不能重复')
        return self


class HostRequest(Contract):
    action_id: str = Field(min_length=1, max_length=100)


TOOLS = [
    tool('run_shell', '在服务端隔离工作区处理文件、计算或运行脚本。必须指定language：shell表示POSIX /bin/sh命令；python表示纯Python 3源码，不能把Python代码标为shell。无网络，不可管理宿主机，不可安装软件。获准附件在 /workspace/input，成果写 /workspace/output 并在 outputs 中声明相对路径；仅使用已取得的本人 record_id。impact_summary说明预计新增、修改、删除哪些文件及失败影响，不能代替权限。本机已授权的隔离工作区自动执行，不重复询问用户；输入是临时副本，不改动服务器原始资料。不要把生成资源称为网络原图。',
         {'language': {'type': 'string', 'enum': ['shell','python']}, 'script': {'type': 'string', 'maxLength': 20000},
          'impact_summary': {'type': 'string', 'maxLength': 2000},
          'inputs': {'type': 'array', 'items': {'type': 'object', 'properties': {'record_id': {'type': 'string'}, 'filename': {'type': 'string'}}, 'required': ['record_id', 'filename'], 'additionalProperties': False}, 'maxItems': 8},
          'outputs': {'type': 'array', 'items': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'kind': {'type': 'string', 'enum': ['image','video','file']}}, 'required': ['path','kind'], 'additionalProperties': False}, 'maxItems': 8},
          'timeout_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 300}}, ['language','script']),
    tool('execute_host_action', '执行本机管理员已登记的宿主管理动作。只能选择 action_id，不接受模型提供的命令、路径或额外参数。服务端会展示固定脚本并请求逐次审批；退出非零或结果不明时不能声称成功。详细输出只在私有执行报告里，不自动发给云模型。',
         {'action_id': {'type': 'string'}}, ['action_id']),
]
MAPPING = {'run_shell':'shell.run@v1', 'execute_host_action':'host.execute@v1'}


def execution_script(request):
    return 'python3 -I -c ' + shlex.quote(request['script']) if request['language'] == 'python' else request['script']


def host_approval_context(action):
    """管理员登记的影响是声明；固定脚本及服务账户权限才是实际执行边界。"""
    return {
        'version': 1, 'source': 'local_administrator',
        'execution_location': '家庭服务器宿主机；工作目录：' + action['cwd'],
        'execution_identity': f'服务器服务账户 UID {os.getuid()} / GID {os.getgid()}',
        'command': '/bin/sh -c ' + shlex.quote(action['script']),
        'constraints': ['只执行本机管理员登记的完整脚本；不接受模型追加参数',
                        f"最长执行 {action['timeout_seconds']} 秒；超时前已发生的修改不会自动撤销",
                        '使用服务账户现有文件和系统权限，不能将工作目录视为隔离边界'],
        'resource_scope': ['脚本可访问服务账户有权访问的宿主机资源；登记的影响范围不是文件访问白名单'],
        'effects': {
            'summary': action['impact_summary'] or '本机管理员尚未登记完整影响；脚本可能新增、修改或删除文件，也可能改变服务和软件。',
            'file_changes': action['file_changes'], 'service_changes': action['service_changes'],
            'uncertainties': ['影响说明由本机管理员登记，未对脚本进行完整静态证明；请结合完整指令确认。',
                              '执行失败或取消时可能已发生部分修改，需要核对执行报告。'],
        },
        'proposal_impact': '',
    }


def sandbox_approval_context(request, versions):
    return {
        'version': 1, 'source': 'sandbox_policy',
        'execution_location': '家庭服务器隔离容器；工作目录：/workspace/output',
        'execution_identity': '容器内非管理员账户 UID 65532 / GID 65532',
        'command': execution_script(request),
        'constraints': ['通过 /bin/sh -s 执行以下完整指令', '关闭网络；宿主文件系统和服务凭据不挂载到容器',
                        '输入为获准资料的临时副本；仅可修改隔离工作区与临时目录，任务结束后清理',
                        f"最长执行 {request['timeout_seconds']} 秒"],
        'resource_scope': [f"输入：{item['record_id']}（版本 {versions[item['record_id']]}）→ /workspace/input/{item['filename']}" for item in request['inputs']]
                          + [f"返回：/workspace/output/{item['path']}" for item in request['outputs']],
        'effects': {
            'summary': '运行脚本并保存私有执行报告；声明的成果文件会作为当前成员的数据附件入库。',
            'file_changes': ['可以新增、修改或删除隔离工作区内的文件；不修改服务器中原始输入资料。'],
            'service_changes': ['不开放宿主机服务管理能力。'],
            'uncertainties': ['脚本的完整运行结果无法预先证明；模型说明仅供核对，不会减少审批要求。'],
        },
        'proposal_impact': request['impact_summary'],
    }


def shell_policy(settings, actor):
    from .shell_configuration import load_policy
    policy = load_policy(settings)
    if not policy.sandbox_enabled or actor.user_id not in policy.sandbox_members:
        raise HTTPException(403, '隔离 Shell 尚未启用或当前成员未获授权')
    if settings.environment == 'production' and policy.sandbox_runtime != 'runsc':
        raise HTTPException(503, '生产 Shell 必须配置并验证 Linux runsc 隔离运行时')
    return policy


def permitted(app, actor, capability):
    try:
        if capability == 'shell.run@v1':
            shell_policy(app.settings, actor)
        else:
            owner(actor)
            from .shell_configuration import load_policy
            policy = load_policy(app.settings)
            if not policy.host_enabled or not policy.host_actions or actor.user_id not in policy.host_members:
                return False
        return True
    except (HTTPException, ValueError):
        return False


def prepare(app, db, actor, body, capability, args):
    """执行镜像、固定动作、来源版本全部绑定到审批摘要，不能在确认后替换。"""
    if body.get('_automation_scope') == 'family':
        raise HTTPException(403, '家庭共享自动化不能执行 Shell 或发布宿主管理输出')
    from .client_actions import read_authorized_record
    versions = {}
    for identifier in set(body.get('record_ids', [])) | set(body.get('_record_dependencies', {})):
        record = read_authorized_record(db, actor, identifier, body)
        if record.owner_id != actor.user_id:
            raise HTTPException(403, 'Shell 不能把其他成员的临时授权资料变成长期文件副本')
        if record.sensitivity == 'SECRET':
            raise HTTPException(403, 'Shell 不能处理秘密资料')
        versions[record.id] = record.version
    try:
        if capability == 'host.execute@v1':
            if body.get('_automation_id'):
                raise HTTPException(403, '宿主管理必须由管理员本人逐次发起，不能嵌入自动化')
            request = HostRequest.model_validate(args)
            from .shell_configuration import host_action
            action = host_action(app.settings, actor, request.action_id)
            return {'action_id': request.action_id, 'execution': action, 'approval_context': host_approval_context(action)}
        request = ShellRequest.model_validate(args)
    except ValueError:
        raise HTTPException(422, 'Shell 参数不符合契约') from None
    policy = shell_policy(app.settings, actor)
    total = 0
    from .media import checked_metadata
    for entry in request.inputs:
        record = read_authorized_record(db, actor, entry.record_id, body)
        if record.owner_id != actor.user_id or record.sensitivity == 'SECRET':
            raise HTTPException(403, 'Shell 输入仅允许本人的非秘密附件')
        total += checked_metadata(app, record)['size']
        if total > 50 * 1024 * 1024:
            raise HTTPException(413, 'Shell 输入文件总量超过 50 MiB')
        versions[record.id] = record.version
    body.setdefault('_record_dependencies', {}).update(versions)
    parameters = request.model_dump()
    return {**parameters, 'approval_context': sandbox_approval_context(parameters, versions), 'execution': {
        'image': policy.sandbox_image, 'runtime': policy.sandbox_runtime,
        'policy_fingerprint': digest(canonical(policy.model_dump())),
        'source_versions': versions, 'network': 'none', 'user': '65532:65532',
    }}


async def execute(app, db, actor, task, body, capability, prepared, invocation_id):
    from .resource_assets import store_asset
    from .media import content_bytes
    from .client_actions import read_authorized_record
    from .result_access import check_dependencies
    original = {key:value for key,value in prepared.items() if key not in {'execution', 'approval_context'}}
    current = prepare(app, db, actor, body, capability, original)
    if current != prepared:
        raise HTTPException(409, 'Shell 配置、来源或命令已变化，需要重新提出任务并审批')
    inputs = {}
    for entry in prepared.get('inputs', []):
        record = read_authorized_record(db, actor, entry['record_id'], body)
        inputs[entry['filename']] = content_bytes(app, record)
    check_dependencies(db, actor, body)

    async def cancelled():
        # 长命令执行中仍接收撤销，使用独立事务避免旧会话缓存。
        with app.db() as session:
            scope(session, actor.user_id, actor.household_id)
            row, device, principal = session.get(Task, task.id), session.get(Device, actor.device_id), session.get(Principal, actor.user_id)
            if not row or row.cancel_requested or row.deadline <= now() or not device or device.revoked or not principal or principal.role != actor.role:
                return True
            try:
                return prepare(app, session, actor, body, capability, original) != prepared
            except (HTTPException, ValueError):
                return True

    if await cancelled():
        raise HTTPException(409, '执行前任务、设备或 Shell 授权已失效')
    body['_shell_dispatched'] = invocation_id
    task.request = app.vault.seal(body, actor.user_id + ':task:' + task.id)
    db.commit()
    from .data import audit
    audit(db, actor, 'shell.dispatched', invocation_id, {'capability': capability})
    db.commit()
    try:
        async with asyncio.timeout(min(body.get('step_timeout_seconds', 120), max(0.001, task.deadline-now()))):
            if capability == 'host.execute@v1':
                from .shell_host import run_host
                result = await run_host(prepared['execution'], cancelled=cancelled)
            else:
                from .shell_sandbox import run_sandbox
                script = execution_script(prepared)
                result = await run_sandbox(script=script, inputs=inputs,
                    outputs=[item['path'] for item in prepared['outputs']], timeout_seconds=prepared['timeout_seconds'],
                    image=prepared['execution']['image'], runtime=prepared['execution']['runtime'], cancelled=cancelled)
    except Exception as exc:
        if getattr(exc, 'dispatched', None) is False:
            body.pop('_shell_dispatched', None)
            task.request = app.vault.seal(body, actor.user_id + ':task:' + task.id)
            db.commit()
        from .shell_sandbox import SandboxError
        if isinstance(exc, SandboxError):
            raise HTTPException(409 if exc.dispatched else 503, exc.detail) from None
        raise
    db.expire_all()
    check_dependencies(db, actor, body)
    if await cancelled():
        raise HTTPException(409, 'Shell 已结束，但任务或授权发生变化，未发布输出')
    source_versions = prepared['execution'].get('source_versions', {})
    provenance = {'origin':'shell_created', 'created_at':now(), 'source_versions':source_versions}
    parts = []
    for item in prepared.get('outputs', []):
        raw = result.get('files', {}).get(item['path'])
        if raw is None:
            continue
        asset = store_asset(app, db, actor, task, invocation_id + ':file:' + item['path'], raw,
            PurePosixPath(item['path']).name, item['kind'], provenance)
        parts.extend(asset['parts'])
    report = '\n'.join([f"退出码：{result['exit_code']}", '输出已截断' if result.get('truncated') else '',
        '\n标准输出：', result.get('stdout', ''), '\n标准错误：', result.get('stderr', '')])
    asset = store_asset(app, db, actor, task, invocation_id + ':report', report.encode(), '命令执行报告.txt', 'file', provenance)
    parts.extend(asset['parts'])
    result.pop('files', None)
    return {**result, 'status':'completed' if result['exit_code'] == 0 else 'failed',
        'text': '命令执行完成。' if result['exit_code'] == 0 else f"命令执行失败，退出码 {result['exit_code']}；请查看执行报告。",
        'parts':parts, 'host_output_private': capability == 'host.execute@v1'}


@router.get('')
async def configuration_status(request: Request, actor=Depends(authenticate)):
    owner(actor)
    from .shell_configuration import status
    value = status(request.app.state.settings, actor, include_scripts=True)
    if value['sandbox']['ready']:
        from .shell_sandbox import sandbox_ready
        ready = await sandbox_ready(value['sandbox']['image'], value['sandbox'].get('runtime'))
        value['sandbox'].update(ready=ready['ready'], reason=ready.get('detail', ready.get('reason', '')))
        if request.app.state.settings.environment == 'production' and value['sandbox'].get('runtime') != 'runsc':
            value['sandbox'].update(ready=False, reason='生产环境需要 Linux runsc 隔离运行时')
    return value
