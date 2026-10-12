"""服务端向成员请求数据；设备只授权、采集和上传，执行与恢复留在服务器。"""
import base64
import math
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field
from sqlalchemy import Boolean, Integer, String, Text, UniqueConstraint, select, or_
from sqlalchemy.orm import Mapped, mapped_column

from .contracts import Contract, DataRecord
from .crypto import canonical, digest
from .db import (Base, Owned, Approval, ConversationTurn, Device, Invocation, Notification,
                 Principal, Record, Task, now, scope, uid)
from .security import Actor, authenticate, own

router = APIRouter(prefix='/api/v1', tags=['客户端请求与成员授权'])
WAITING_STATUS = 'WAITING_CLIENT'
ACTION_SECONDS = 24 * 60 * 60
DATA_KINDS = {'choose_files', 'choose_photos', 'capture_location', 'read_health', 'provide_text', 'choose_option', 'authorize_records', 'data.share'}
ACTION_KINDS = DATA_KINDS | {'cloud.disclose', 'member.notify', 'android.assist'}
TERMINAL_TASKS = {'SUCCEEDED', 'FAILED', 'CANCELED', 'NEEDS_RECONCILIATION'}


def display_purpose(kind, purpose):
    """兼容历史卡片，仅替换确知为系统生成的通用模板。"""
    if kind == 'cloud.disclose' and purpose in {
        '当前任务需要披露敏感或仅本地资料，请单独确认',
        '允许本次任务向指定云模型披露所选资料的脱敏内容',
    }:
        return '请确认本次所选资料的云端披露。'
    if purpose == '成员请求你为本次任务提供资料。仅你主动选择的内容会用于这次任务，不会改成家庭共享。':
        return member_request_purpose(kind)
    return purpose


def member_request_purpose(kind):
    subject = {'choose_files': '选择文件', 'choose_photos': '选择照片', 'capture_location': '提供位置',
               'read_health': '提供健康数据', 'provide_text': '补充信息', 'choose_option': '选择一个选项',
               'authorize_records': '授权所选资料', 'data.share': '选择共享资料'}.get(kind, '提供资料')
    return '成员请求你为本次任务' + subject + '。'


def _legacy_request_copy_matches(app, action, actor, kind, purpose, parameters, target):
    """旧系统模板换文案时复用原待办，原授权载荷和摘要保持不变。"""
    if action.status != 'PENDING' or action.expires_at <= now() or action.kind != kind or action.owner_id != target or action.household_id != actor.household_id:
        return False
    previous = payload_of(app, action)
    if previous.get('parameters') != parameters:
        return False
    old_purpose = previous.get('purpose')
    if action.request_hash != digest(canonical({'kind': kind, 'purpose': old_purpose, 'parameters': parameters, 'target': target})):
        return False
    if target != actor.user_id:
        if kind == 'cloud.disclose':
            return old_purpose == '允许本次任务向指定云模型披露所选资料的脱敏内容' and purpose == '请确认将你为本次任务提供的资料发送至云模型。'
        return (kind in DATA_KINDS and old_purpose == '成员请求你为本次任务提供资料。仅你主动选择的内容会用于这次任务，不会改成家庭共享。'
                and purpose == member_request_purpose(kind))
    if kind != 'cloud.disclose' or old_purpose != '当前任务需要披露敏感或仅本地资料，请单独确认':
        return False
    if purpose == '请确认本次资料的云端披露。':
        return True
    prefix, suffix = '本次将向云模型发送', '，请确认。'
    if not purpose.startswith(prefix) or not purpose.endswith(suffix):
        return False
    from .cloud_gateway import PRIVACY_CATEGORIES
    allowed = {value for code, value in PRIVACY_CATEGORIES.items() if code != 'unknown'} | {
        '附件图像（图像内容无法可靠脱敏）', '位置信息', '敏感资料', '标记为仅本地处理的资料',
        '本地检测标记为敏感的内容（未返回具体类别）', '你为本次任务提供的资料',
    }
    reasons = purpose[len(prefix):-len(suffix)].split('、')
    return all(reason in allowed for reason in reasons) and reasons == sorted(set(reasons))


class ClientAction(Owned, Base):
    __tablename__ = 'client_actions'
    __table_args__ = (UniqueConstraint('requester_id', 'task_id', 'request_key'),)
    requester_id: Mapped[str] = mapped_column(String, index=True)
    task_id: Mapped[str] = mapped_column(String, index=True)
    invocation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    request_key: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    payload: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default='PENDING')
    created_at: Mapped[float] = mapped_column(default=now)
    expires_at: Mapped[float]
    responded_at: Mapped[float | None] = mapped_column(nullable=True)
    response_key: Mapped[str | None] = mapped_column(String, nullable=True)
    response_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    notification_id: Mapped[str | None] = mapped_column(String, nullable=True)


class TaskDataGrant(Owned, Base):
    __tablename__ = 'task_data_grants'
    __table_args__ = (UniqueConstraint('action_id', 'record_id'),)
    requester_id: Mapped[str] = mapped_column(String, index=True)
    task_id: Mapped[str] = mapped_column(String, index=True)
    action_id: Mapped[str] = mapped_column(String, index=True)
    record_id: Mapped[str] = mapped_column(String, index=True)
    record_version: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[float]
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class Mention(Contract):
    member_id: str = Field(min_length=1, max_length=100)


def validate_mentions(db, actor, mentions):
    if not isinstance(mentions, list) or len(mentions) > 8:
        raise HTTPException(422, '每条消息最多指定八位成员')
    result = []
    for value in mentions:
        try:
            parsed = Mention.model_validate(value).member_id
        except Exception:
            raise HTTPException(422, '成员引用格式无效') from None
        member = db.get(Principal, parsed)
        if not member or member.household_id != actor.household_id or member.role not in {'adult', 'child', 'minor', 'infrastructure_owner'}:
            raise HTTPException(404, '成员不存在于当前家庭')
        if not any(item['member_id'] == parsed for item in result):
            result.append({'member_id': parsed})
    return result


@contextmanager
def principal_scope(db, user_id, household):
    previous = (db.info.get('user_id'), db.info.get('household_id'))
    db.flush()
    scope(db, user_id, household)
    try:
        yield
        db.flush()
    finally:
        if previous[0] and previous[1]:
            scope(db, *previous)


def payload_of(app, action):
    return app.vault.open(action.payload, action.owner_id + ':client-action:' + action.id)


def allowed_target(db, actor, task_body, target_member_id):
    target = actor.user_id if target_member_id in {None, '', 'self'} else target_member_id
    member = db.get(Principal, target)
    if not member or member.household_id != actor.household_id:
        raise HTTPException(404, '成员不存在于当前家庭')
    mentions = validate_mentions(db, actor, task_body.get('_mentions', []))
    if target != actor.user_id and target not in {item['member_id'] for item in mentions}:
        raise HTTPException(403, '只能请求本条消息明确指定的成员')
    if target != actor.user_id and task_body.get('_automation_scope') == 'family':
        raise HTTPException(403, '家庭自动化不能请求其他成员的私人资料')
    return target


def _instant(value):
    if not isinstance(value, str) or len(value) > 50:
        raise ValueError()
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError()
    return parsed.timestamp()


class HealthRequestValidationError(HTTPException):
    """时间参数尚未派发即可修正；不能用此类型捕获其他权限或执行错误。"""
    def __init__(self, code, detail):
        super().__init__(422, detail)
        self.code = code


def _health_instant(values, key):
    import math
    label = '开始时间' if key == 'start_at' else '结束时间'
    if key not in values or values[key] is None or values[key] == '':
        raise HealthRequestValidationError('health_time_missing', '健康查询缺少' + label)
    value = values[key]
    if not isinstance(value, str) or len(value) > 50:
        raise HealthRequestValidationError('health_time_invalid_format', '健康查询' + label + '必须是带时区的 ISO 8601 时间')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise HealthRequestValidationError('health_time_invalid_format', '健康查询' + label + '不是有效的 ISO 8601 时间') from None
    if parsed.tzinfo is None:
        raise HealthRequestValidationError('health_time_missing_timezone', '健康查询' + label + '缺少时区')
    try:
        instant = parsed.timestamp()
    except (ValueError, OverflowError):
        raise HealthRequestValidationError('health_time_invalid_format', '健康查询' + label + '超出可解析范围') from None
    if not math.isfinite(instant):
        raise HealthRequestValidationError('health_time_non_finite', '健康查询' + label + '不是有限时间')
    return instant


def validate_planned_health_request(arguments):
    """仅预检健康请求，不改日期、不创建待办、不推断或授予权限。"""
    if isinstance(arguments, dict) and arguments.get('kind') == 'read_health':
        parameters_for('read_health', arguments.get('parameters'))


def parameters_for(kind, values):
    if kind not in ACTION_KINDS or not isinstance(values, dict) or len(canonical(values)) > 12000:
        raise HTTPException(422, '客户端请求类型或参数无效')
    fields = {
        'choose_files': {'accepted_mime_types', 'max_count', 'max_bytes'},
        'choose_photos': {'accepted_mime_types', 'max_count', 'max_bytes'},
        'capture_location': {'precision', 'max_age_seconds'},
        'read_health': {'types', 'start_at', 'end_at'},
        'provide_text': {'prompt', 'max_length'}, 'choose_option': {'prompt', 'options'},
        'authorize_records': {'record_ids'}, 'data.share': {'record_ids'},
        'cloud.disclose': {'scope_hash', 'record_versions', 'provider_id'},
        'member.notify': {'summary'},
        'android.assist': {'workspace_id', 'reason', 'code'},
    }
    if set(values) - fields[kind]:
        raise HTTPException(422, '客户端请求含不允许的参数')
    result = dict(values)
    if kind in {'choose_files', 'choose_photos'}:
        result.setdefault('max_count', 5); result.setdefault('max_bytes', 10 * 1024 * 1024)
        result.setdefault('accepted_mime_types', [])
        if type(result['max_count']) is not int or not 1 <= result['max_count'] <= 20 or type(result['max_bytes']) is not int or not 1 <= result['max_bytes'] <= 20 * 1024 * 1024:
            raise HTTPException(422, '文件数量或大小超过允许范围')
        if not isinstance(result['accepted_mime_types'], list) or len(result['accepted_mime_types']) > 10 or any(not isinstance(item, str) or len(item) > 100 for item in result['accepted_mime_types']):
            raise HTTPException(422, '文件类型范围无效')
    if kind == 'capture_location':
        result.setdefault('precision', 'coarse'); result.setdefault('max_age_seconds', 300)
        if not isinstance(result['precision'],str) or result['precision'] not in {'coarse', 'precise'} or type(result['max_age_seconds']) is not int or not 0 <= result['max_age_seconds'] <= 3600:
            raise HTTPException(422, '位置精度或新鲜度范围无效')
    if kind == 'read_health':
        if not isinstance(result.get('types'), list) or not result['types'] or len(result['types']) > len(HEALTH_UNITS) or any(not isinstance(value,str) or value not in HEALTH_UNITS for value in result['types']) or len(set(result['types']))!=len(result['types']):
            raise HTTPException(422, '健康指标必须来自支持的指标清单，且不能重复')
        start, end = _health_instant(result, 'start_at'), _health_instant(result, 'end_at')
        if end <= start:
            raise HealthRequestValidationError('health_time_invalid_order', '健康查询结束时间必须晚于开始时间')
        if end - start > 31 * 86400:
            raise HealthRequestValidationError('health_time_range_exceeded', '单次健康查询不能超过 31 天')
        if end > now() + 300:
            raise HealthRequestValidationError('health_time_future_end', '健康查询结束时间在未来；查询今天只能截至当前时间，不能填写今晚23:59:59')
    if kind == 'provide_text':
        result.setdefault('max_length', 4000); result.setdefault('prompt', '')
        if re.search(r'密码|口令|私钥|验证码|恢复码|助记词|api[ _-]?key|access[ _-]?token',str(result['prompt']),re.I):
            raise HTTPException(403,'凭据只能通过安全配置入口提供，不能在对话补充框中收集')
        if type(result['max_length']) is not int or not 1 <= result['max_length'] <= 10000 or not isinstance(result['prompt'], str) or len(result['prompt']) > 1000:
            raise HTTPException(422, '补充文本参数无效')
    if kind == 'choose_option':
        options=result.get('options')
        if not isinstance(result.get('prompt'),str) or not 1<=len(result['prompt'])<=1000 or not isinstance(options,list) or not 1<=len(options)<=8:
            raise HTTPException(422,'选项请求需要提示与一至八个选项')
        if any(not isinstance(item,dict) or set(item)!={'id','label'} or not isinstance(item['id'],str) or not 1<=len(item['id'])<=100 or not isinstance(item['label'],str) or not 1<=len(item['label'])<=200 for item in options):
            raise HTTPException(422,'选项标识或标题无效')
        if len({item['id'] for item in options})!=len(options):raise HTTPException(422,'选项标识不能重复')
    if kind in {'authorize_records', 'data.share'}:
        result.setdefault('record_ids', [])
        if not isinstance(result['record_ids'], list) or len(result['record_ids']) > 20 or any(not isinstance(value, str) or not 1 <= len(value) <= 100 for value in result['record_ids']):
            raise HTTPException(422, '记录范围无效')
    if kind == 'cloud.disclose':
        if not isinstance(result.get('scope_hash'), str) or not 16 <= len(result['scope_hash']) <= 200 or not isinstance(result.get('record_versions'), dict) or len(result['record_versions']) > 100:
            raise HTTPException(422, '云端披露范围无效')
        if any(not isinstance(key, str) or type(version) is not int or version < 1 for key, version in result['record_versions'].items()):
            raise HTTPException(422, '披露记录版本无效')
        if not isinstance(result.get('provider_id'), str) or not 1 <= len(result['provider_id']) <= 100:
            raise HTTPException(422, '披露目标无效')
    if kind == 'android.assist':
        if set(result) != {'workspace_id', 'reason', 'code'} or any(not isinstance(value, str) or not 1 <= len(value) <= 1000 for value in result.values()):
            raise HTTPException(422, '安卓协助范围无效')
    if kind == 'member.notify' and (not isinstance(result.get('summary'), str) or not 1 <= len(result['summary']) <= 1000):
        raise HTTPException(422, '通知摘要无效')
    return result


def clarification_arguments(values):
    """模型显式提问转换成现有本人待办；不从普通回复猜测等待状态。"""
    question = values.get('question')
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= 1000:
        raise HTTPException(422, '补充问题需要说明本次缺少的信息')
    question = question.strip()
    options = values.get('options', [])
    if not isinstance(options, list):
        raise HTTPException(422, '补充问题的选项必须是列表')
    # 与普通补充文本共用凭据校验，不能通过选择题收集密码或验证码。
    parameters_for('provide_text', {'prompt': question})
    if options:
        parameters = parameters_for('choose_option', {'prompt': question, 'options': options})
        for option in parameters['options']:
            parameters_for('provide_text', {'prompt': option['label']})
        kind = 'choose_option'
    else:
        parameters = parameters_for('provide_text', {'prompt': question, 'max_length': 4000})
        kind = 'provide_text'
    return {'target_member_id': 'self', 'kind': kind, 'purpose': question, 'parameters': parameters}


def _notice(app, db, action, status='PENDING'):
    from .notifications import create_notification
    with principal_scope(db, action.owner_id, action.household_id):
        create_notification(db, action.household_id, action.owner_id, 'client-action:' + action.id,
                            'client.action', 'personal', status)
        action.notification_id = db.scalar(select(Notification.id).where(Notification.owner_id == action.owner_id,
            Notification.event_key == 'client-action:' + action.id))


def create_request(app, db, actor, task, kind, purpose, parameters, *, target_member_id=None, invocation=None, request_key=None, _android_core=False):
    if kind == 'android.assist' and not _android_core:
        raise HTTPException(403, '安卓协助只能由执行器在真实阻塞时创建')
    if task.owner_id != actor.user_id or task.household_id != actor.household_id or task.status in TERMINAL_TASKS or task.cancel_requested:
        raise HTTPException(409, '任务不能再请求客户端数据')
    if not isinstance(purpose, str) or not 1 <= len(purpose) <= 1000:
        raise HTTPException(422, '需要明确的请求用途')
    body = app.vault.open(task.request, actor.user_id + ':task:' + task.id)
    if kind == 'cloud.disclose':
        target = actor.user_id if target_member_id in {None, '', 'self'} else target_member_id
        member = db.get(Principal,target)
        if not member or member.household_id != actor.household_id:
            raise HTTPException(404,'披露授权成员不存在')
    else:
        target = allowed_target(db, actor, body, target_member_id)
    if kind == 'member.notify':
        raise HTTPException(403, '成员通知必须通过明确审批的通知能力执行')
    parameters = parameters_for(kind, parameters)
    if target!=actor.user_id and kind!='cloud.disclose':
        purpose=member_request_purpose(kind)
        if isinstance(parameters.get('prompt'),str):parameters['prompt']=parameters['prompt'][:160]
    if kind == 'cloud.disclose':
        if target != actor.user_id and not parameters['record_versions']:
            raise HTTPException(403,'不能让其他成员批准当前用户自己的聊天')
        for record_id, version in parameters['record_versions'].items():
            record = read_authorized_record(db,actor,record_id,body)
            if record.owner_id != target or record.version != version or record.sensitivity == 'SECRET':
                raise HTTPException(403,'云端授权范围必须属于批准者本人且版本有效')
        if target != actor.user_id:
            purpose = '请确认将你为本次任务提供的资料发送至云模型。'
    request_key = request_key or (invocation.id if invocation else digest(canonical({'kind':kind, 'parameters':parameters, 'target':target})))
    fingerprint = digest(canonical({'kind':kind, 'purpose':purpose, 'parameters':parameters, 'target':target}))
    existing = db.scalar(select(ClientAction).where(ClientAction.requester_id == actor.user_id, ClientAction.task_id == task.id, ClientAction.request_key == request_key))
    if existing:
        if existing.request_hash != fingerprint and not _legacy_request_copy_matches(app, existing, actor, kind, purpose, parameters, target):
            raise HTTPException(409, '请求标识已绑定其他内容')
        if existing.status == 'PENDING' and existing.expires_at > now():
            return {'status':'waiting_client', 'client_action_id':existing.id, 'expires_at':existing.expires_at}
        raise HTTPException(409, '该客户端请求已处理，请依据原结果继续')
    if body.get('_client_wait'):
        raise HTTPException(409, '任务已有等待中的客户端请求')
    action = ClientAction(id=uid(), owner_id=target, household_id=actor.household_id, requester_id=actor.user_id,
        task_id=task.id, invocation_id=invocation.id if invocation else None, request_key=request_key,
        request_hash=fingerprint, kind=kind, payload='', expires_at=now()+ACTION_SECONDS)
    action.payload = app.vault.seal({'purpose':purpose,'parameters':parameters}, target+':client-action:'+action.id)
    with principal_scope(db, target, actor.household_id):
        db.add(action); db.flush(); _notice(app,db,action)
    body['_task_id'] = task.id
    body['_client_wait'] = {'action_id':action.id,'remaining_seconds':max(1,min(3600,task.deadline-now()))}
    task.request = app.vault.seal(body,actor.user_id+':task:'+task.id)
    task.status, task.deadline = WAITING_STATUS, action.expires_at
    if invocation:
        invocation.status = WAITING_STATUS
    from .data import audit, emit
    audit(db,actor,'client.request',action.id,{'kind':kind});emit(db,actor,'task.updated',task.id)
    db.flush()
    return {'status':'waiting_client','client_action_id':action.id,'expires_at':action.expires_at}


def _grant_record(db, actor, grant):
    task=db.get(Task,grant.task_id,populate_existing=True)
    if not task or task.owner_id!=actor.user_id or task.cancel_requested or task.status=='CANCELED':
        raise HTTPException(403,'原任务已取消，临时资料授权不能继续使用')
    source = db.get(Principal, grant.owner_id,populate_existing=True)
    if not source or source.household_id != actor.household_id or grant.household_id != actor.household_id:
        raise HTTPException(403, '资料授权所属家庭已变化')
    action = db.get(ClientAction, grant.action_id,populate_existing=True)
    if grant.revoked or grant.expires_at <= now() or not action or action.status != 'RESPONDED' or action.expires_at <= now() or action.task_id!=grant.task_id or action.requester_id!=grant.requester_id or action.owner_id!=grant.owner_id:
        raise HTTPException(403, '本次任务的资料授权已失效')
    from .data import read_record
    with principal_scope(db, source.id, actor.household_id):
        record = read_record(db,Actor(source.id,actor.household_id,'task-grant-reader',source.role),grant.record_id)
        if record.owner_id!=grant.owner_id or record.version != grant.record_version or record.sensitivity == 'SECRET' or (record.owner_id != actor.user_id and record.kind.startswith('memory.')):
            raise HTTPException(403, '资料版本或分类已经变化，请重新授权')
        return record


def _derived_record(db,actor,record_id,payload):
    reference=payload.get('_derived_task_sources',{}).get(record_id)
    if not isinstance(reference,dict) or set(reference)-{'source_id','version','source_version'}:
        raise HTTPException(403,'衍生资料来源绑定无效')
    source_id=reference.get('source_id');version=reference.get('version')
    source_version=reference.get('source_version',payload.get('_record_dependencies',{}).get(source_id))
    if not isinstance(source_id,str) or source_id==record_id or source_id in payload.get('_derived_task_sources',{}) or type(version) is not int or type(source_version) is not int or min(version,source_version)<1:
        raise HTTPException(403,'衍生资料版本或来源绑定无效')
    source=read_authorized_record(db,actor,source_id,payload)
    if source.kind not in {'document.file','document.import','photo.file','video.file'} or source.version!=source_version or source.sensitivity=='SECRET':
        raise HTTPException(403,'衍生资料的原件已变化或不允许读取')
    from .data import read_record
    def selected(reader):
        derived=read_record(db,reader,record_id)
        if derived.owner_id!=source.owner_id or derived.household_id!=actor.household_id or derived.source!='document_parse' or derived.source_id!=source.id or derived.kind!='document.parsed' or derived.version!=version or derived.sensitivity=='SECRET':
            raise HTTPException(403,'衍生正文关联、版本或密级已经变化')
        return derived
    # 只有明确的私人任务授权允许临时切换owner；家庭共享依然使用请求者自己的RLS。
    if source_id in payload.get('_task_grants',{}):
        member=db.get(Principal,source.owner_id,populate_existing=True)
        with principal_scope(db,member.id,actor.household_id):
            result=selected(Actor(member.id,actor.household_id,'task-derived-reader',member.role))
    else:result=selected(actor)
    read_authorized_record(db,actor,source_id,payload)
    return result


def read_authorized_record(db, actor, record_id, payload):
    from .data import read_record
    grant_id = payload.get('_task_grants', {}).get(record_id)
    if not grant_id and record_id in payload.get('_derived_task_sources',{}):return _derived_record(db,actor,record_id,payload)
    if grant_id:
        scope(db,actor.user_id,actor.household_id)
        grant = db.scalar(select(TaskDataGrant).where(TaskDataGrant.id == grant_id, TaskDataGrant.requester_id == actor.user_id,
            TaskDataGrant.task_id == payload.get('_task_id'), TaskDataGrant.record_id == record_id, TaskDataGrant.household_id == actor.household_id).execution_options(populate_existing=True))
        if not grant or payload.get('_automation_scope') == 'family':
            raise HTTPException(403, '本次任务无权读取私人资料')
        record=_grant_record(db,actor,grant)
        reference=payload.get('_derived_task_sources',{}).get(record_id)
        if reference:
            if not isinstance(reference,dict) or record.kind!='document.parsed' or record.source!='document_parse' or reference.get('version')!=record.version or reference.get('source_id')!=record.source_id:
                raise HTTPException(403,'直接授权的解析正文来源绑定无效')
            with principal_scope(db,record.owner_id,actor.household_id):
                source=read_record(db,Actor(record.owner_id,actor.household_id,'derived-provenance-check','service'),record.source_id)
                if source.owner_id!=record.owner_id or source.kind not in {'document.file','document.import','photo.file','video.file'} or source.sensitivity=='SECRET' or source.version!=reference.get('source_version'):
                    raise HTTPException(403,'直接授权的解析正文原件已变化')
        return record
    return read_record(db,actor,record_id)


@contextmanager
def authorized_source_scope(db,actor,record_id,task_payload):
    # 调用者只能把固定 record_id 交给只读媒体帮助函数；这里不授权任意主体切换。
    source=read_authorized_record(db,actor,record_id,task_payload)
    version=source.version
    reference=task_payload.get('_derived_task_sources',{}).get(record_id,{})
    grant_source=record_id if record_id in task_payload.get('_task_grants',{}) else reference.get('source_id')
    if grant_source not in task_payload.get('_task_grants',{}):
        yield actor
    else:
        member=db.get(Principal,source.owner_id,populate_existing=True)
        with principal_scope(db,member.id,actor.household_id):
            yield Actor(member.id,actor.household_id,'task-grant-read:'+task_payload['_task_id'],member.role)
    current=read_authorized_record(db,actor,record_id,task_payload)
    if current.version!=version:raise HTTPException(409,'读取期间资料或授权已变化')


def check_task_grants(db, actor, payload):
    scope(db,actor.user_id,actor.household_id)
    for action_id in payload.get('_cloud_consents', []):
        action=db.scalar(select(ClientAction).where(ClientAction.id==action_id,ClientAction.requester_id==actor.user_id,
            ClientAction.task_id==payload.get('_task_id'),ClientAction.household_id==actor.household_id,ClientAction.kind=='cloud.disclose'))
        source=db.get(Principal,action.owner_id) if action else None
        if not action or not source or source.household_id!=actor.household_id or action.status!='RESPONDED' or action.expires_at<=now():
            raise HTTPException(403,'本次任务云端披露授权已撤回或过期')
    for identifier in payload.get('_task_grants', {}):
        read_authorized_record(db,actor,identifier,payload)


def cloud_consent_status(app, db, actor, task_id, scope_hash, record_versions, *, owner_id=None):
    scope(db,actor.user_id,actor.household_id)
    rows = db.scalars(select(ClientAction).where(ClientAction.owner_id == (owner_id or actor.user_id),
        ClientAction.requester_id == actor.user_id, ClientAction.task_id == task_id, ClientAction.kind == 'cloud.disclose').order_by(ClientAction.created_at.desc()))
    for action in rows:
        params = payload_of(app,action)['parameters']
        if params['scope_hash'] != scope_hash or params['record_versions'] != record_versions:
            continue
        if action.expires_at <= now():return 'expired'
        if action.status == 'RESPONDED':
            try:
                with principal_scope(db,actor.user_id,actor.household_id):
                    task = own(db,Task,task_id,actor)
                    if task.cancel_requested or task.status=='CANCELED':return 'revoked'
                    payload = app.vault.open(task.request,actor.user_id+':task:'+task.id)
                    for rid, version in record_versions.items():
                        if read_authorized_record(db,actor,rid,payload).version != version:return 'expired'
            except HTTPException:return 'revoked'
            return 'approved'
        return {'PENDING':'pending','DENIED':'denied','REVOKED':'revoked','CANCELED':'revoked','EXPIRED':'expired'}.get(action.status,'missing')
    return 'missing'


def cloud_consent(app, db, actor, task_id, scope_hash, record_versions, *, owner_id=None):
    return cloud_consent_status(app,db,actor,task_id,scope_hash,record_versions,owner_id=owner_id) == 'approved'


def _notice_dependencies_valid(app,db,action,payload=None):
    if action.status=='REVOKED':return False
    dependencies=(payload or payload_of(app,action)).get('dependencies')
    if not dependencies:return True
    user=db.get(Principal,action.requester_id)
    if not user or user.household_id!=action.household_id:return False
    try:
        with principal_scope(db,user.id,user.household_id):
            reader=Actor(user.id,user.household_id,'member-message-reader',user.role)
            for record_id,version in dependencies.get('_record_dependencies',{}).items():
                record=read_authorized_record(db,reader,record_id,dependencies)
                if record.version!=version or record.sensitivity=='SECRET':return False
            check_task_grants(db,reader,dependencies)
    except HTTPException:return False
    return True


def _hide_member_notice(app,db,action):
    from .notifications import create_notification
    from .data import emit
    with principal_scope(db,action.owner_id,action.household_id):
        action.status='REVOKED'
        payload=payload_of(app,action);payload['parameters']={'summary':'关联资料已变化或授权已撤回，消息内容已隐藏。'}
        action.payload=app.vault.seal(payload,action.owner_id+':client-action:'+action.id)
        create_notification(db,action.household_id,action.owner_id,'client-action:'+action.id+':withdrawn','client.action','personal','REVOKED')
        user=db.get(Principal,action.owner_id)
        emit(db,Actor(user.id,user.household_id,'notice-withdrawal',user.role),'client.action.updated',action.id)


def invalidate_member_notices(app,db,actor,task_id):
    rows=list(db.scalars(select(ClientAction).where(ClientAction.requester_id==actor.user_id,ClientAction.task_id==task_id,
        ClientAction.household_id==actor.household_id,ClientAction.kind=='member.notify',ClientAction.status=='RESPONDED')))
    for action in rows:
        if not _notice_dependencies_valid(app,db,action):_hide_member_notice(app,db,action)


def refresh_member_notices(app):
    # 读取接口立即校验；后台按页轮转使已打开客户端也收到失效通知。
    positions=getattr(app,'member_notice_positions',None)
    if positions is None:positions=app.member_notice_positions={}
    with app.db() as db:members=[(p.id,p.household_id) for p in db.scalars(select(Principal))]
    for user_id,household in members:
        with app.db() as db:
            scope(db,user_id,household)
            previous=positions.get(user_id,'')
            rows=list(db.scalars(select(ClientAction).where(ClientAction.owner_id==user_id,ClientAction.kind=='member.notify',
                ClientAction.status=='RESPONDED',ClientAction.id>previous).order_by(ClientAction.id).limit(100)))
            for action in rows:
                if not _notice_dependencies_valid(app,db,action):_hide_member_notice(app,db,action)
            positions[user_id]=rows[-1].id if len(rows)==100 else ''
            db.commit()


def _automatic_capture_allowed(app, db, action, actor):
    if action.kind not in {'capture_location', 'read_health'} or action.status != 'PENDING' or action.expires_at <= now():
        return False
    if action.owner_id != actor.user_id or action.requester_id != actor.user_id:
        return False
    device = db.get(Device, actor.device_id)
    if not device or device.revoked or device.user_id != actor.user_id or not device.auto_review_enabled:
        return False
    task = db.get(Task, action.task_id)
    if not task or task.owner_id != actor.user_id or task.household_id != actor.household_id or task.cancel_requested or task.status != 'WAITING_CLIENT':
        return False
    payload = app.vault.open(task.request, actor.user_id + ':task:' + task.id)
    return payload.get('device_id') == actor.device_id and payload.get('_client_wait', {}).get('action_id') == action.id


def _view(app, db, action, actor):
    payload = payload_of(app,action)
    withdrawn=action.kind=='member.notify' and not _notice_dependencies_valid(app,db,action,payload)
    if withdrawn:payload={**payload,'parameters':{'summary':'关联资料已变化或授权已撤回，消息内容已隐藏。'}}
    requester = db.get(Principal,action.requester_id)
    conversation = None
    if action.requester_id == actor.user_id:
        conversation = db.scalar(select(ConversationTurn.conversation_id).where(ConversationTurn.task_id == action.task_id))
    effective = 'REVOKED' if withdrawn else ('EXPIRED' if action.status == 'PENDING' and action.expires_at <= now() else action.status)
    outcome = {}
    if action.kind == 'read_health' and effective == 'RESPONDED' and action.response:
        receipt = _response_receipt(app, action)
        outcome = {key: receipt[key] for key in ('data_status', 'note') if key in receipt}
    return {'id':action.id,'kind':action.kind,'status':effective,'purpose':display_purpose(action.kind, payload['purpose']),
        'parameters':payload['parameters'],'requested_by':{'id':action.requester_id,'name':requester.name if requester else '已离开的成员'},
        'target_member_id':action.owner_id,'scope':'personal','created_at':action.created_at,'expires_at':action.expires_at,
        'task_id':action.task_id if action.requester_id == actor.user_id else None,'conversation_id':conversation,
        'automatic_capture_allowed':_automatic_capture_allowed(app,db,action,actor),
        'notification_id':action.notification_id,'can_respond':action.owner_id==actor.user_id and effective=='PENDING',
        'can_revoke':action.owner_id==actor.user_id and action.status=='RESPONDED' and action.kind not in {'member.notify', 'android.assist'}, **outcome}


def task_actions(app, db, actor, task_id):
    own(db,Task,task_id,actor)
    rows=db.scalars(select(ClientAction).where(ClientAction.task_id==task_id,ClientAction.requester_id==actor.user_id,
        ClientAction.household_id==actor.household_id).order_by(ClientAction.created_at))
    return [_view(app,db,row,actor) for row in rows]


@router.get('/client-actions')
def listing(request:Request, actor:Actor=Depends(authenticate), before:float|None=Query(None,gt=0), limit:int=Query(50,ge=1,le=100), notification_id:str|None=None):
    with request.app.state.db() as db:
        scope(db,actor.user_id,actor.household_id)
        query = select(ClientAction).where(ClientAction.owner_id == actor.user_id,ClientAction.household_id == actor.household_id)
        if before is not None:query=query.where(ClientAction.created_at < before)
        if notification_id:
            notification=own(db,Notification,notification_id,actor)
            parts=notification.event_key.split(':')
            action_reference=parts[1] if len(parts)>=2 and parts[0]=='client-action' else ''
            query=query.where(or_(ClientAction.notification_id==notification_id,ClientAction.id==action_reference))
        rows=list(db.scalars(query.order_by(ClientAction.created_at.desc(),ClientAction.id).limit(limit+1)))
        more=len(rows)>limit;rows=rows[:limit]
        return {'items':[_view(request.app.state,db,row,actor) for row in rows],'has_more':more,'next_before':rows[-1].created_at if more else None}


@router.get('/client-actions/{action_id}')
def detail(action_id:str,request:Request,actor:Actor=Depends(authenticate)):
    with request.app.state.db() as db:return _view(request.app.state,db,own(db,ClientAction,action_id,actor),actor)


class ResponseInput(Contract):
    idempotency_key:str=Field(min_length=8,max_length=100)
    record_ids:list[str]=Field(default_factory=list,max_length=20)
    text:str|None=Field(default=None,max_length=10000)
    automatic_review:bool=Field(default=False, strict=True)


class DenialInput(Contract):
    idempotency_key:str=Field(min_length=8,max_length=100)
    reason:str=Field(default='用户拒绝',max_length=300)


def _locked_action(db, actor, action_id):
    action=own(db,ClientAction,action_id,actor)
    with principal_scope(db,action.requester_id,actor.household_id):
        task=db.scalar(select(Task).where(Task.id==action.task_id,Task.owner_id==action.requester_id,Task.household_id==actor.household_id).with_for_update().execution_options(populate_existing=True))
    action=db.scalar(select(ClientAction).where(ClientAction.id==action_id,ClientAction.owner_id==actor.user_id).with_for_update().execution_options(populate_existing=True))
    return action,task


def _resume(app,db,action,task,result,*,failed=False):
    from .data import emit
    member=db.get(Principal,action.requester_id)
    if not task or not member or member.household_id!=action.household_id:
        raise HTTPException(409,'原任务已不存在或成员已离开')
    with principal_scope(db,action.requester_id,action.household_id):
        body=app.vault.open(task.request,task.owner_id+':task:'+task.id)
        wait=body.get('_client_wait')
        if task.cancel_requested or task.status in TERMINAL_TASKS or not wait or wait.get('action_id')!=action.id:
            raise HTTPException(409,'原任务已取消、完成或等待条件已变化')
        body.setdefault('_derived_task_sources',{}).update(result.pop('_derived_sources',{}))
        if action.invocation_id:
            invocation=db.get(Invocation,action.invocation_id)
            if not invocation or invocation.task_id!=task.id or invocation.status!=WAITING_STATUS:
                raise HTTPException(409,'原执行步骤已变化')
            invocation.status='FAILED' if failed else 'SUCCEEDED'
            invocation.result=app.vault.seal(result,task.owner_id+':invocation-result:'+invocation.id)
        grants=list(db.scalars(select(TaskDataGrant).where(TaskDataGrant.action_id==action.id,TaskDataGrant.requester_id==task.owner_id)))
        for grant in grants:
            body.setdefault('_task_grants',{})[grant.record_id]=grant.id
            body.setdefault('_record_dependencies',{})[grant.record_id]=grant.record_version
        for record in result.get('records',[]):
            body.setdefault('_record_dependencies',{})[record['id']]=record['version']
            if record['id'] not in body.setdefault('record_ids',[]):body['record_ids'].append(record['id'])
        if len(body.get('record_ids',[]))>40:raise HTTPException(413,'当前任务资料超过40项，请分成独立任务')
        if action.kind=='cloud.disclose' and not failed:
            if action.id not in body.setdefault('_cloud_consents',[]):body['_cloud_consents'].append(action.id)
        body.pop('_client_wait',None)
        body['_task_id']=task.id
        task.request=app.vault.seal(body,task.owner_id+':task:'+task.id)
        task.status='FAILED' if failed else 'RECEIVED'
        task.error='客户端未提供所需授权或资料' if failed else None
        task.deadline=now()+max(1,min(3600,wait['remaining_seconds']))
        emit(db,Actor(task.owner_id,action.household_id,'client-action-resume',member.role),'task.updated',task.id)


def _response_receipt(app,action):
    result=app.vault.open(action.response,action.owner_id+':client-response:'+action.id) if action.response else {'id':action.id}
    return {**result,'status':action.status}


def _check_response(action, key, fingerprint):
    if action.response_key:
        if action.response_key==key and action.response_hash==fingerprint:return True
        raise HTTPException(409,'请求已由其他响应处理')
    if action.status!='PENDING' or action.expires_at<=now():
        raise HTTPException(409,'请求已失效或不再等待响应')
    return False


HEALTH_UNITS={'sleep':'category','steps':'count','heart_rate':'count/min','body_mass':'kg',
    'resting_heart_rate':'count/min','heart_rate_variability':'ms','respiratory_rate':'count/min',
    'oxygen_saturation':'%','active_energy':'kcal','basal_energy':'kcal','exercise_minutes':'min',
    'walking_running_distance':'m','body_temperature':'degC','body_fat_percentage':'%'}
HEALTH_MAX_SAMPLES_PER_RECORD = 5000
HEALTH_MAX_SAMPLES_PER_REQUEST = 20000
HEALTH_SAMPLE_FIELDS = {'type','sample_id','start_at','end_at','value','unit'}
HEALTH_SAMPLE_METADATA = {'source_name','source_bundle','source_version','source_product','timezone',
    'original_start_at','original_end_at'}


def validate_health_record(record,payload,parameters):
    try:
        requested=set(parameters['types']);lower,upper=_instant(parameters['start_at']),_instant(parameters['end_at'])
        start,end=_instant(payload['start_at']),_instant(payload['end_at'])
        if not lower<=start<=end<=upper:raise ValueError()
        if record.kind=='health.sleep':
            if requested!={'sleep'}:raise ValueError()
            samples=[{'type':'sleep','sample_id':record.id,'start_at':payload['start_at'],'end_at':payload['end_at'],'value':payload['value'],'unit':'category'}]
        elif record.kind=='health.snapshot':
            if not isinstance(payload.get('types'),list) or set(payload['types'])!=requested or len(payload['types'])!=len(requested):raise ValueError()
            samples=payload.get('samples')
            if not isinstance(samples,list) or len(samples)>HEALTH_MAX_SAMPLES_PER_RECORD:raise ValueError()
            if payload.get('status') not in (None,'available' if samples else 'no_data'):raise ValueError()
            if 'schema_version' in payload and (type(payload['schema_version']) is not int or payload['schema_version'] != 2):raise ValueError()
            for flag in ('query_complete','truncated'):
                if flag in payload and type(payload[flag]) is not bool:raise ValueError()
            if payload.get('query_complete') is True and payload.get('truncated') is True:raise ValueError()
            chunks={'capture_id','chunk_index','chunk_count'}&set(payload)
            if chunks:
                if len(chunks)!=3 or not isinstance(payload['capture_id'],str) or not 1<=len(payload['capture_id'])<=100:raise ValueError()
                if type(payload['chunk_index']) is not int or type(payload['chunk_count']) is not int or not 1<=payload['chunk_count']<=20 or not 0<=payload['chunk_index']<payload['chunk_count']:raise ValueError()
        else:raise ValueError()
        seen=set()
        for sample in samples:
            if not isinstance(sample,dict) or not HEALTH_SAMPLE_FIELDS<=set(sample) or set(sample)-HEALTH_SAMPLE_FIELDS-HEALTH_SAMPLE_METADATA:raise ValueError()
            kind=sample['type'];identifier=sample['sample_id'];value=sample['value']
            if kind not in requested or sample['unit']!=HEALTH_UNITS[kind] or not isinstance(identifier,str) or not 1<=len(identifier)<=200 or (kind,identifier) in seen:raise ValueError()
            seen.add((kind,identifier))
            sample_start,sample_end=_instant(sample['start_at']),_instant(sample['end_at'])
            if not start<=sample_start<=sample_end<=end:raise ValueError()
            original={'original_start_at','original_end_at'}&set(sample)
            if original:
                if len(original)!=2 or kind!='sleep':raise ValueError()
                original_start,original_end=_instant(sample['original_start_at']),_instant(sample['original_end_at'])
                if not original_start<=sample_start<=sample_end<=original_end or original_end-original_start>31*86400:raise ValueError()
                if sample_start!=max(start,original_start) or sample_end!=min(end,original_end):raise ValueError()
            for field in HEALTH_SAMPLE_METADATA-{'original_start_at','original_end_at'}:
                if field in sample and (not isinstance(sample[field],str) or len(sample[field])>200 or any(ord(char)<32 for char in sample[field])):raise ValueError()
            if type(value) not in (int,float) or not math.isfinite(value):raise ValueError()
            if kind=='sleep' and (not float(value).is_integer() or not 0<=value<=5):raise ValueError()
            if kind=='steps' and (not float(value).is_integer() or not 0<=value<=1_000_000_000):raise ValueError()
            if kind in {'heart_rate','resting_heart_rate','respiratory_rate'} and not 0<value<=1000:raise ValueError()
            if kind=='body_mass' and not 0<value<=2000:raise ValueError()
            if kind in {'oxygen_saturation','body_fat_percentage'} and not 0<=value<=100:raise ValueError()
            if kind in {'heart_rate_variability','active_energy','basal_energy','exercise_minutes','walking_running_distance'} and not 0<=value<=1_000_000_000:raise ValueError()
            if kind=='body_temperature' and not -273.15<=value<=1000:raise ValueError()
    except (KeyError,ValueError,TypeError,OverflowError):
        raise HTTPException(422,'健康快照类型、时间、单位或样本数不符合请求范围') from None


def health_outcome(app, records, parameters):
    """只根据实际样本描述可用性，HealthKit 空结果不能证明用户拒绝读取授权。"""
    requested = parameters['types']
    available = set()
    count = 0
    ranges = []
    samples_by_id = {}
    all_samples = []
    query_complete = True
    completeness_known = True
    captures = {}
    for record in records:
        payload = app.vault.open(record.payload, record.owner_id + ':record:' + record.id)
        validate_health_record(record, payload, parameters)
        ranges.append((_instant(payload['start_at']), _instant(payload['end_at'])))
        if record.kind == 'health.sleep':
            available.add('sleep'); count += 1
            completeness_known = False
            samples = [{'type':'sleep','sample_id':record.id,'start_at':payload['start_at'],
                'end_at':payload['end_at'],'value':payload['value'],'unit':'category'}]
        else:
            samples = payload['samples']
            available.update(sample['type'] for sample in samples)
            count += len(samples)
            query_complete = query_complete and payload.get('query_complete', True) and not payload.get('truncated', False)
            completeness_known = completeness_known and 'query_complete' in payload
            if 'capture_id' in payload:
                capture = captures.setdefault(payload['capture_id'], {'count':payload['chunk_count'], 'parts':set(),
                    'window':(payload['start_at'],payload['end_at']), 'types':set(payload['types'])})
                if capture['count']!=payload['chunk_count'] or capture['window']!=(payload['start_at'],payload['end_at']) or capture['types']!=set(payload['types']) or payload['chunk_index'] in capture['parts']:
                    raise HTTPException(422, '健康采集分块不一致或重复，请重新采集')
                capture['parts'].add(payload['chunk_index'])
        for sample in samples:
            key = (sample['type'], sample['sample_id'])
            previous = samples_by_id.get(key)
            if previous:
                invariant = (HEALTH_SAMPLE_FIELDS | HEALTH_SAMPLE_METADATA) - {'start_at','end_at'}
                # 分块可重复同一条睡眠 UUID，但不能用同一 UUID 替换原值、来源或不相交区间。
                if any(previous.get(field)!=sample.get(field) for field in invariant) or max(_instant(previous['start_at']),_instant(sample['start_at']))>min(_instant(previous['end_at']),_instant(sample['end_at'])):
                    raise HTTPException(422, '相同健康样本标识的内容或来源冲突，请重新采集')
                samples_by_id[key] = {**previous,
                    'start_at':min(previous['start_at'],sample['start_at'],key=_instant),
                    'end_at':max(previous['end_at'],sample['end_at'],key=_instant)}
            else:
                samples_by_id[key] = sample
        all_samples.extend(samples)
    if count > HEALTH_MAX_SAMPLES_PER_REQUEST:
        raise HTTPException(422, '本次健康请求超过20000条样本，请按较短时间范围继续查询')
    query_complete = query_complete and all(len(capture['parts'])==capture['count'] for capture in captures.values())
    missing = [kind for kind in requested if kind not in available]
    # 查询窗口与样本时间不同：全部指标有样本，也不能把一天窗口说成已读取整周。
    lower, upper = _instant(parameters['start_at']), _instant(parameters['end_at'])
    covered_until = lower
    for start, end in sorted(ranges):
        if start > covered_until:
            break
        covered_until = max(covered_until, end)
    time_range_complete = covered_until >= upper and query_complete
    status = 'no_data' if not count else ('partial' if missing or not time_range_complete else 'available')
    notes = {
        'no_data': '本次没有读到健康样本。可能是所选时间内没有记录，或系统未开放读取权限；无法据此确定是否拒绝授权。不得把空样本当作零值，也不要重复请求同一范围，请说明缺失并让用户选择是否重新采集。',
        'partial': '已提供部分健康数据。没有样本的指标可能没有记录或未开放读取权限，不能当作零值或断言授权被拒绝；仅依据实际样本继续本任务。',
        'available': '已收到本次请求范围内的健康样本，可继续原任务；这不代表持续读取授权或实时监测。',
    }
    note = notes[status]
    if not time_range_complete:
        note += ' 本次提供的时间范围未完整覆盖请求，仅能分析已提供的时间段，不能声称已取得整个请求期间的数据。'
    if not query_complete:
        note += ' 本次采集未完整结束或缺少分块；需要完整结果时按更短时间段继续采集，不能从现有样本推断全部记录。'
    elif not completeness_known:
        note += ' 旧版采集未记录查询是否完整结束，以下统计覆盖已保存样本，不能据此保证已读到设备上的全部记录。'
    from .health_summary import summarize
    return {'data_status': status, 'note': note, 'health': {
        'requested_types': requested, 'available_types': [kind for kind in requested if kind in available],
        'missing_types': missing, 'sample_count': count,
        'requested_start_at': parameters['start_at'], 'requested_end_at': parameters['end_at'],
        'time_range_complete': time_range_complete,
        'query_complete': query_complete if completeness_known or not query_complete else None,
        'summary': summarize(all_samples, parameters['start_at']),
        'provided_ranges': [{'start_at': datetime.fromtimestamp(start, timezone.utc).isoformat(),
            'end_at': datetime.fromtimestamp(end, timezone.utc).isoformat()} for start, end in sorted(set(ranges))],
    }}


def _settled_health_request(app, db, actor, task, target, parameters):
    """同一任务不因模型再次请求而重复弹出已经处理过的健康授权卡片。"""
    rows = db.scalars(select(ClientAction).where(ClientAction.requester_id == actor.user_id,
        ClientAction.task_id == task.id, ClientAction.owner_id == target,
        ClientAction.household_id == actor.household_id, ClientAction.kind == 'read_health',
        ClientAction.status == 'RESPONDED', ClientAction.expires_at > now()).order_by(ClientAction.created_at.desc()))
    for action in rows:
        payload = payload_of(app, action)
        previous = payload.get('health_reuse', {}).get('original_parameters', payload['parameters'])
        if (not set(parameters['types']) <= set(previous['types'])
            or _instant(previous['end_at']) <= _instant(parameters['start_at'])
            or _instant(previous['start_at']) >= _instant(parameters['end_at'])):
            continue
        receipt = _response_receipt(app, action)
        if receipt.get('data_status') == 'no_data':
            if (_instant(parameters['start_at']) < _instant(previous['start_at'])
                or _instant(parameters['end_at']) > _instant(previous['end_at'])):
                continue
            return {'status': 'no_data', 'data_status': 'no_data', 'records': [],
                'client_action_id': action.id, 'note': receipt['note']}
        if receipt.get('data_status') in {'available', 'partial'}:
            from .data import serialize
            body = app.vault.open(task.request, actor.user_id + ':task:' + task.id)
            records = [read_authorized_record(db, actor, identifier, body) for identifier in receipt['record_ids']]
            from .health_reuse import project_outcome
            outcome = project_outcome(app, records, parameters, queries=receipt.get('health_queries', []))
            return {'status': 'succeeded', 'records': [serialize(row, app.vault) for row in records],
                'client_action_id': action.id, 'health_queries': receipt.get('health_queries', []), **outcome,
                'note': outcome['note'] + ' 同一请求已处理，请使用本次结果继续，不要重复弹出授权卡片。'}
    return None


def _response_notice(db, actor, action):
    from .data import emit
    if action.notification_id:
        notification = db.get(Notification, action.notification_id)
        if notification and notification.owner_id == actor.user_id:
            notification.status = action.status
    emit(db, actor, 'client.action.updated', action.id)


def _records_for_response(app,db,actor,action,body):
    from .data import ingest,read_record,serialize
    parameters=payload_of(app,action)['parameters']
    ids=list(dict.fromkeys(body.record_ids))
    if action.kind=='cloud.disclose':
        if ids or body.text is not None:raise HTTPException(422,'云端确认不能修改披露范围')
        for identifier,version in parameters['record_versions'].items():
            record=own(db,Record,identifier,actor)
            if record.deleted or record.version!=version or record.sensitivity=='SECRET':
                raise HTTPException(409,'待披露资料已变化，需要重新请求授权')
        return []
    if action.kind=='choose_option':
        selected=next((item for item in parameters['options'] if item['id']==body.text),None)
        if ids or selected is None:raise HTTPException(422,'请选择请求中已列出的选项')
        return [ingest(db,actor,DataRecord(source='client_action',source_id=action.id,kind='client_input.choice',version=1,
            cloud_policy='REDACT_AND_ALLOW',payload={'option_id':selected['id'],'label':selected['label']}),app.vault)]
    if action.kind=='provide_text':
        if ids or not body.text or len(body.text)>parameters['max_length']:raise HTTPException(422,'需要范围内的补充文本')
        from .privacy import ensure_model_safe
        ensure_model_safe(body.text)
        row=ingest(db,actor,DataRecord(source='client_action',source_id=action.id,kind='client_input.text',version=1,cloud_policy='REDACT_AND_ALLOW',payload={'text':body.text}),app.vault)
        return [row]
    if body.text is not None or not ids:raise HTTPException(422,'请选择至少一项自己的资料')
    if len(ids)>parameters.get('max_count',20):raise HTTPException(422,'选择数量超过请求范围')
    allowed=set(parameters.get('record_ids',[]))
    if allowed and not set(ids)<=allowed:raise HTTPException(403,'选择的资料超出本次请求范围')
    records=[]
    for identifier in ids:
        row=read_record(db,actor,identifier)
        if row.owner_id!=actor.user_id or row.sensitivity=='SECRET' or row.kind.startswith('memory.'):
            raise HTTPException(403,'只能提供自己的非秘密原始资料，不能转授他人数据或聊天记忆')
        payload=serialize(row,app.vault)['payload']
        if action.kind=='choose_files' and row.kind not in {'document.file','document.import'}:raise HTTPException(422,'请求只接受文件')
        if action.kind=='choose_photos' and row.kind not in {'photo.selected','photo.file'}:raise HTTPException(422,'请求只接受照片')
        if action.kind in {'choose_files','choose_photos'}:
            size=payload.get('size')
            if 'content_base64' in payload:
                try:size=len(base64.b64decode(payload['content_base64'],validate=True))
                except Exception:raise HTTPException(422,'附件编码无效') from None
            if type(size) is not int or not 0<=size<=parameters['max_bytes']:raise HTTPException(422,'附件大小超出请求范围')
        if action.kind=='read_health':validate_health_record(row,payload,parameters)
        if action.kind=='capture_location':
            if row.kind!='location.point':raise HTTPException(422,'请求只接受位置记录')
            if parameters['precision']=='coarse' and payload.get('precision')!='coarse':raise HTTPException(422,'请提供粗略位置，不能自动扩大精度')
            latitude,longitude=payload.get('latitude'),payload.get('longitude')
            if any(type(value) not in (int,float) or not math.isfinite(value) for value in (latitude,longitude)) or not -90<=latitude<=90 or not -180<=longitude<=180:
                raise HTTPException(422,'位置坐标无效')
            if parameters['precision']=='coarse' and any(abs(value*100-round(value*100))>1e-6 for value in (latitude,longitude)):
                raise HTTPException(422,'粗略位置最多保留两位小数')
            try:
                sampled=_instant(payload.get('timestamp',payload.get('observed_at')))
                if not now()-parameters['max_age_seconds']-30<=sampled<=now()+30:raise ValueError()
            except (ValueError,TypeError,OverflowError):raise HTTPException(422,'位置记录已过期') from None
        records.append(row)
    return records


@router.post('/client-actions/{action_id}/respond')
def respond(action_id:str,body:ResponseInput,request:Request,actor:Actor=Depends(authenticate)):
    app=request.app.state
    with app.db() as db:
        result = respond_in_transaction(app, db, actor, action_id, body)
        db.commit()
        return result


def respond_in_transaction(app, db, actor, action_id, body):
    fingerprint=digest(canonical({'decision':'respond',**body.model_dump(exclude={'automatic_review'} if not body.automatic_review else set())}))
    from .sync_order import lock_changes
    scope(db,actor.user_id,actor.household_id);lock_changes(db)
    action,task=_locked_action(db,actor,action_id)
    if body.automatic_review:
        from .auto_review import require_enabled
        require_enabled(app, db, actor, task)
        if action.owner_id != actor.user_id or action.requester_id != actor.user_id:
            raise HTTPException(403, '自动审核不能代替其他成员响应')
        if action.kind not in {'cloud.disclose', 'authorize_records', 'data.share', 'capture_location', 'read_health'}:
            raise HTTPException(403, '本次请求需要用户提供真实输入')
    if _check_response(action,body.idempotency_key,fingerprint):return _response_receipt(app,action)
    if action.kind == 'android.assist':
        raise HTTPException(422, '安卓协助只能通过继续或取消处理，不能提交资料或密码')
    records=_records_for_response(app,db,actor,action,body)
    outcome = health_outcome(app, records, payload_of(app, action)['parameters']) if action.kind == 'read_health' else {}
    health_queries = ([{**payload_of(app, action)['parameters'],
        'query_complete':outcome['health']['query_complete'],
        'time_range_complete':outcome['health']['time_range_complete']}]
        if action.kind == 'read_health' else [])
    # 空快照是采集结果，不把它当作可供分析的健康资料或额外的云端披露来源。
    if outcome.get('data_status') == 'no_data':
        records = []
    reuse = payload_of(app, action).get('health_reuse') if action.kind == 'read_health' else None
    if reuse:
        # 新采集仍按卡片的缩减范围严格校验；旧来源不能因一次响应而更新授权或版本。
        from .health_reuse import project_outcome
        task_body = app.vault.open(task.request, task.owner_id + ':task:' + task.id)
        requester = db.get(Principal, action.requester_id)
        if not requester or requester.household_id != action.household_id:
            raise HTTPException(403, '原请求成员已离开家庭')
        request_actor = Actor(requester.id, action.household_id, 'health-reuse', requester.role)
        saved = []
        for identifier, version in reuse['record_versions'].items():
            row = read_authorized_record(db, request_actor, identifier, task_body)
            if row.owner_id != action.owner_id or row.version != version or row.sensitivity == 'SECRET':
                raise HTTPException(409, '此前已提供的健康资料已变化，请重新查询')
            saved.append(row)
        records = list({row.id: row for row in [*saved, *records]}.values())
        outcome = project_outcome(app, records, reuse['original_parameters'], queries=health_queries)
    action.status='RESPONDED';action.responded_at=now()
    for record in records:
        if record.owner_id==action.requester_id or (reuse and record.id in reuse['record_versions']):continue
        db.add(TaskDataGrant(owner_id=actor.user_id,household_id=actor.household_id,requester_id=action.requester_id,
            task_id=action.task_id,action_id=action.id,record_id=record.id,record_version=record.version,expires_at=action.expires_at))
    result={'status':'no_data' if outcome.get('data_status') == 'no_data' else 'succeeded',
        'client_action_id':action.id,'record_ids':[row.id for row in records], **outcome,
        **({'health_queries':health_queries} if health_queries else {})}
    from .data import read_record,serialize
    from .documents import checked_payload
    for record in records:
        if record.kind=='document.parsed' and record.source=='document_parse':
            value=serialize(record,app.vault)['payload'];checked_payload(db,actor,record,app.vault,value)
            parent=read_record(db,actor,record.source_id)
            if parent.sensitivity=='SECRET':raise HTTPException(403,'秘密原件的解析内容不能用于任务授权')
            result.setdefault('_derived_sources',{})[record.id]={'source_id':parent.id,'source_version':parent.version,'version':record.version}
    from .data import audit
    from .device_sync import sync_projection
    result['records']=[sync_projection(row,app.vault) for row in records]
    db.flush();_resume(app,db,action,task,result)
    action.response_key,action.response_hash=body.idempotency_key,fingerprint
    action.response=app.vault.seal({'id':action.id,'status':'RESPONDED','record_ids':[row.id for row in records],
        **({'health_queries':health_queries} if health_queries else {}),
        **{key:outcome[key] for key in ('data_status','note') if key in outcome}},actor.user_id+':client-response:'+action.id)
    _response_notice(db, actor, action)
    audit(db,actor,'client.respond',action.id,{'kind':action.kind,'records':len(records), **({'automatic':True,'device_id':actor.device_id} if body.automatic_review else {})})
    return _response_receipt(app,action)


@router.post('/client-actions/{action_id}/deny')
def deny(action_id:str,body:DenialInput,request:Request,actor:Actor=Depends(authenticate)):
    app=request.app.state;fingerprint=digest(canonical({'decision':'deny',**body.model_dump()}))
    with app.db() as db:
        action,task=_locked_action(db,actor,action_id)
        if _check_response(action,body.idempotency_key,fingerprint):return _response_receipt(app,action)
        if action.kind == 'android.assist':
            from .android_assistance import cancel_assistance
            cancel_assistance(app, db, actor, action, task)
            action.status = 'CANCELED'
        else:
            action.status = 'DENIED'
            _resume(app,db,action,task,{'status':'denied','client_action_id':action.id},failed=True)
        action.responded_at=now()
        action.response_key,action.response_hash=body.idempotency_key,fingerprint
        action.response=app.vault.seal({'id':action.id,'status':action.status},actor.user_id+':client-response:'+action.id)
        _response_notice(db, actor, action)
        from .data import audit
        audit(db,actor,'client.deny',action.id)
        db.commit();return _response_receipt(app,action)


def _revoke(app,db,action):
    from .data import emit
    with principal_scope(db,action.owner_id,action.household_id):
        grants=list(db.scalars(select(TaskDataGrant).where(TaskDataGrant.action_id==action.id,TaskDataGrant.owner_id==action.owner_id)))
        for grant in grants:grant.revoked=True
        action.status='REVOKED';db.flush()
    with principal_scope(db,action.requester_id,action.household_id):
        user=db.get(Principal,action.requester_id)
        if user and user.household_id==action.household_id:
            actor=Actor(user.id,user.household_id,'grant-revocation',user.role)
            task=db.get(Task,action.task_id)
            if task:
                task.result=None
                emit(db,actor,'task.updated',task.id)
            for grant in grants:emit(db,actor,'record.revoked',grant.record_id)
            if not grants:emit(db,actor,'record.revoked',action.id)
            invalidate_member_notices(app,db,actor,action.task_id)


@router.post('/client-actions/{action_id}/revoke')
def revoke(action_id:str,request:Request,actor:Actor=Depends(authenticate)):
    with request.app.state.db() as db:
        from .sync_order import lock_changes
        scope(db,actor.user_id,actor.household_id);lock_changes(db)
        action,task=_locked_action(db,actor,action_id)
        if action.status=='REVOKED':return {'id':action.id,'status':'REVOKED'}
        if action.status!='RESPONDED' or action.kind in {'member.notify', 'android.assist'}:raise HTTPException(409,'没有可撤回的数据授权')
        _revoke(request.app.state,db,action)
        from .data import audit
        audit(db,actor,'client.revoke',action.id);db.commit()
        return {'id':action.id,'status':'REVOKED'}


@router.get('/tasks/{task_id}/data/{record_id}')
def task_record(task_id:str,record_id:str,request:Request,actor:Actor=Depends(authenticate)):
    from .data import serialize
    with request.app.state.db() as db:
        task=own(db,Task,task_id,actor)
        payload=request.app.state.vault.open(task.request,actor.user_id+':task:'+task.id)
        return serialize(read_authorized_record(db,actor,record_id,payload),request.app.state.vault)


@router.get('/tasks/{task_id}/assets/{record_id}')
def task_asset(task_id:str,record_id:str,request:Request,actor:Actor=Depends(authenticate)):
    from .media import asset_view
    from .data import read_record
    app=request.app.state
    with app.db() as db:
        task=own(db,Task,task_id,actor);payload=app.vault.open(task.request,actor.user_id+':task:'+task.id)
        with authorized_source_scope(db,actor,record_id,payload) as reader:
            result=asset_view(app,db,reader,read_record(db,reader,record_id))
            if reader.user_id!=actor.user_id:result['processing'].pop('result_record_id',None)
        return result


@router.get('/tasks/{task_id}/assets/{record_id}/content')
def task_asset_content(task_id:str,record_id:str,request:Request,offset:int=Query(0,ge=0),length:int=Query(1024*1024,ge=1,le=1024*1024),actor:Actor=Depends(authenticate)):
    from .media import content_bytes,checked_metadata
    from .data import read_record
    from fastapi.responses import Response
    from urllib.parse import quote
    app=request.app.state
    with app.db() as db:
        task=own(db,Task,task_id,actor);payload=app.vault.open(task.request,actor.user_id+':task:'+task.id)
        with authorized_source_scope(db,actor,record_id,payload) as reader:
            source=read_record(db,reader,record_id);metadata=checked_metadata(app,source)
            raw=content_bytes(app,source,offset=offset,length=length)
        return Response(raw,status_code=200 if len(raw)==metadata['size'] else 206,media_type=metadata['mime_type'],headers={
            'Content-Range':f"bytes {offset}-{offset+len(raw)-1}/{metadata['size']}",'Accept-Ranges':'bytes',
            'Content-Disposition':"attachment; filename*=UTF-8''"+quote(metadata['name'],safe=''),
            'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})


@router.get('/tasks/{task_id}/assets/{record_id}/thumbnail')
def task_asset_thumbnail(task_id:str,record_id:str,request:Request,actor:Actor=Depends(authenticate)):
    from .media import thumbnail,checked_metadata
    from .data import read_record
    app=request.app.state
    with app.db() as db:
        task=own(db,Task,task_id,actor);payload=app.vault.open(task.request,actor.user_id+':task:'+task.id)
        with authorized_source_scope(db,actor,record_id,payload) as reader:
            checked_metadata(app,read_record(db,reader,record_id))
            result=thumbnail(record_id,request,reader)
        return result


def cancel_task_actions(app,db,actor,task):
    if task.owner_id!=actor.user_id or task.household_id!=actor.household_id:
        raise HTTPException(404,'任务不存在')
    from .data import emit
    scope(db,actor.user_id,actor.household_id)
    rows=list(db.scalars(select(ClientAction).where(ClientAction.requester_id==actor.user_id,
        ClientAction.task_id==task.id,ClientAction.household_id==actor.household_id,ClientAction.status=='PENDING')))
    for candidate in rows:
        with principal_scope(db,candidate.owner_id,actor.household_id):
            action=db.scalar(select(ClientAction).where(ClientAction.id==candidate.id,ClientAction.owner_id==candidate.owner_id).with_for_update())
            if action.status=='PENDING':
                action.status='CANCELED'
                target=db.get(Principal,action.owner_id)
                emit(db,Actor(target.id,target.household_id,'client-action-cancel',target.role),'client.action.updated',action.id)
                if action.notification_id:
                    notification=db.get(Notification,action.notification_id)
                    if notification:notification.status='CANCELED'
    return len(rows)


def expire_requests(app):
    """供 Worker 调用，客户端离线不会阻止过期或授权撤回。"""
    with app.db() as db:members=[(p.id,p.household_id,p.role) for p in db.scalars(select(Principal))]
    for identifier,household,role in members:
        with app.db() as db:
            from .sync_order import lock_changes
            scope(db,identifier,household);lock_changes(db)
            actions=list(db.scalars(select(ClientAction).where(ClientAction.owner_id==identifier,ClientAction.expires_at<=now(),ClientAction.status.in_(['PENDING','RESPONDED']),ClientAction.kind!='member.notify',or_(ClientAction.kind!='android.assist',ClientAction.status=='PENDING')).limit(100)))
            for row in actions:
                action,task=_locked_action(db,Actor(identifier,household,'expiry',role),row.id)
                if action.status=='RESPONDED':
                    if action.kind != 'android.assist':
                        _revoke(app,db,action);action.status='EXPIRED'
                elif action.status=='PENDING':
                    if task and task.status==WAITING_STATUS and not task.cancel_requested:
                        if action.kind == 'android.assist':
                            from .android_assistance import cancel_assistance
                            cancel_assistance(app, db, Actor(identifier,household,'expiry',role), action, task, expired=True)
                        else:
                            _resume(app,db,action,task,{'status':'expired','client_action_id':action.id},failed=True)
                    action.status='EXPIRED'
            db.commit()
    refresh_member_notices(app)


def execute(app,db,actor,task,invocation,capability,arguments,*,request_key=None):
    body=app.vault.open(task.request,actor.user_id+':task:'+task.id)
    if capability=='client.request@v1':
        if set(arguments)!={'target_member_id','kind','purpose','parameters'} or arguments['kind'] not in DATA_KINDS:
            raise HTTPException(422,'客户端请求参数无效')
        target=allowed_target(db,actor,body,arguments['target_member_id'])
        parameters=parameters_for(arguments['kind'],arguments['parameters'])
        if arguments['kind'] == 'read_health':
            settled = _settled_health_request(app, db, actor, task, target, parameters)
            if settled is not None:
                return settled
        if arguments['kind']=='capture_location' and target==actor.user_id and parameters['precision']=='coarse':
            snapshot=body.get('_client_context',{})
            sample=snapshot.get('location')
            if sample and snapshot.get('availability',{}).get('location')=='available' and body.get('_client_context_expires_at',0)>now():
                try:age=now()-_instant(sample['observed_at'])
                except (KeyError,ValueError,TypeError):age=float('inf')
                if 0<=age<=min(parameters['max_age_seconds'],900):
                    return {'status':'succeeded','location':sample,'source':'current_message_context','note':'这是消息内已授权的新鲜粗略采样，不是持续定位'}
        if arguments['kind']=='read_health':
            from .data import serialize
            from .health_reuse import refresh_requested, select_saved
            reused = None if refresh_requested(body) else select_saved(app, db, actor, body, parameters, target)
            if reused:
                records, outcome = reused
                missing = outcome['health']['uncollected_types']
                if not missing:
                    return {'status':'succeeded','records':[serialize(row,app.vault) for row in records],
                        'source':'saved_authorized_server_record', **outcome}
                labels = {'sleep':'睡眠','steps':'步数','heart_rate':'心率','resting_heart_rate':'静息心率',
                    'heart_rate_variability':'心率变异性','respiratory_rate':'呼吸频率','oxygen_saturation':'血氧',
                    'active_energy':'活动能量','basal_energy':'静息能量','exercise_minutes':'运动时间',
                    'walking_running_distance':'步行跑步距离','body_temperature':'体温','body_fat_percentage':'体脂率','body_mass':'体重'}
                existing = [labels.get(kind,kind) for kind in outcome['health']['queried_types']]
                purpose = ('已使用服务器保存的' + '、'.join(existing) + '资料，本次只补充' + '、'.join(labels.get(kind,kind) for kind in missing) + '。')
                requested = {**parameters, 'types':missing}
                result = create_request(app,db,actor,task,'read_health',purpose,requested,
                    target_member_id=arguments['target_member_id'],invocation=invocation,request_key=request_key)
                action = db.get(ClientAction,result['client_action_id'])
                payload = payload_of(app,action)
                payload['health_reuse'] = {'record_versions':{row.id:row.version for row in records},
                    'original_parameters':parameters}
                action.payload = app.vault.seal(payload,action.owner_id+':client-action:'+action.id)
                return result
        return create_request(app,db,actor,task,arguments['kind'],arguments['purpose'],arguments['parameters'],target_member_id=arguments['target_member_id'],invocation=invocation,request_key=request_key)
    if capability=='member.read@v1':
        if set(arguments)!={'target_member_id','record_id','purpose'}:raise HTTPException(422,'成员数据请求参数无效')
        target=allowed_target(db,actor,body,arguments['target_member_id'])
        record_id=arguments['record_id']
        if record_id:
            try:
                row=read_authorized_record(db,actor,record_id,body)
                if row.owner_id!=target:raise HTTPException(403,'资料不属于指定成员')
                from .data import serialize
                return {'records':[serialize(row,app.vault)],'status':'succeeded'}
            except HTTPException as exc:
                if exc.status_code!=404:raise
        return create_request(app,db,actor,task,'data.share',arguments['purpose'],{'record_ids':[record_id] if record_id else []},target_member_id=target,invocation=invocation)
    if capability=='member.notify@v1':
        if set(arguments)!={'target_member_id','summary'}:raise HTTPException(422,'成员通知参数无效')
        target=allowed_target(db,actor,body,arguments['target_member_id'])
        params=parameters_for('member.notify',{'summary':arguments['summary']})
        for record_id,grant_id in body.get('_task_grants',{}).items():
            grant=db.scalar(select(TaskDataGrant).where(TaskDataGrant.id==grant_id,TaskDataGrant.requester_id==actor.user_id,TaskDataGrant.task_id==task.id))
            if not grant or grant.owner_id!=target:raise HTTPException(403,'不能把他人仅授权给本任务的私人资料转发给其他成员')
            read_authorized_record(db,actor,record_id,body)
        approval=db.scalar(select(Approval).where(Approval.invocation_id==invocation.id,Approval.owner_id==actor.user_id,
            Approval.decision=='APPROVED',Approval.expires_at>now(),Approval.arguments_hash==invocation.arguments_hash))
        if not approval:raise HTTPException(403,'定向通知需要本人明确确认')
        existing=db.scalar(select(ClientAction).where(ClientAction.requester_id==actor.user_id,ClientAction.task_id==task.id,ClientAction.request_key==invocation.id))
        if existing:
            if existing.request_hash!=digest(canonical(arguments)):raise HTTPException(409,'通知幂等参数已变化')
            return {'status':'succeeded','client_action_id':existing.id}
        row=ClientAction(id=uid(),owner_id=target,household_id=actor.household_id,requester_id=actor.user_id,task_id=task.id,
            invocation_id=invocation.id,request_key=invocation.id,request_hash=digest(canonical(arguments)),kind='member.notify',
            payload='',status='RESPONDED',expires_at=now()+ACTION_SECONDS,responded_at=now())
        row.payload=app.vault.seal({'purpose':'成员发来的消息','parameters':params,'dependencies':{'_task_id':task.id,'_task_grants':body.get('_task_grants',{}),'_record_dependencies':body.get('_record_dependencies',{}),'_derived_task_sources':body.get('_derived_task_sources',{})}},target+':client-action:'+row.id)
        with principal_scope(db,target,actor.household_id):db.add(row);db.flush();_notice(app,db,row,'RESPONDED')
        return {'status':'succeeded','client_action_id':row.id}
    raise HTTPException(422,'未知客户端能力')


def task_documents_search(app,db,actor,body,query):
    """只在本任务已授权原件及其正文内做有界字面检索，不伪称向量搜索。"""
    if not isinstance(query,str) or not 1<=len(query.strip())<=2000:
        raise HTTPException(422,'需要有效的文档查询')
    from .privacy import ensure_model_safe
    ensure_model_safe(query)
    from .data import accessible,serialize
    from .documents import checked_payload
    source_ids=set(body.get('record_ids',[]))|set(body.get('_task_grants',{}))|set(body.get('_historical_record_ids',[]))
    source_ids.update(value['source_id'] for identifier,value in body.get('_derived_task_sources',{}).items() if identifier not in body.get('_task_grants',{}) and isinstance(value,dict) and isinstance(value.get('source_id'),str))
    if len(source_ids)>40:raise HTTPException(413,'本任务资料范围超过检索上限')
    pattern=re.compile(re.escape(query),re.I);matches=[]
    for source_id in sorted(source_ids):
        source=read_authorized_record(db,actor,source_id,body)
        if source.kind not in {'document.file','document.import','photo.file','video.file','document.parsed'}:continue
        with authorized_source_scope(db,actor,source_id,body) as reader:
            child=source if source.kind=='document.parsed' else db.scalar(accessible(db,reader).where(Record.source=='document_parse',Record.source_id==source.id,
                Record.owner_id==source.owner_id,Record.kind=='document.parsed').execution_options(populate_existing=True))
            if child is None:continue
            if child.sensitivity=='SECRET':raise HTTPException(403,'秘密解析正文不能用于检索')
            value=serialize(child,app.vault)['payload']
            checked_payload(db,reader,child,app.vault,value)
            if source.kind!='document.parsed' and value.get('source_version')!=source.version:raise HTTPException(409,'解析来源版本已过期')
            content=value.get('markdown')
            if not isinstance(content,str) or len(content.encode())>3*1024*1024:raise HTTPException(422,'解析正文无效或超过检索限制')
            ensure_model_safe(content)
            if source.kind!='document.parsed':body.setdefault('_derived_task_sources',{})[child.id]={'source_id':source.id,'source_version':source.version,'version':child.version}
            body.setdefault('_record_dependencies',{}).update({source.id:source.version,child.id:child.version})
            match=pattern.search(content)
            if match:
                start=max(0,match.start()-200);end=min(len(content),start+1000)
                title=value.get('name','文档')
                matches.append({'record_id':child.id,'version':child.version,'start':start,'end':end,
                    'title':title[:200] if isinstance(title,str) else '文档','excerpt':content[start:end]})
        if len(matches)>=5:break
    for result in matches:read_authorized_record(db,actor,result['record_id'],body)
    return {'mode':'authorized_literal','matches':matches,'reranking':'not_configured'}


def task_rehydrate(app,db,actor,body,result):
    """旧excerpt不作为真相；每个匹配都回到本任务当前授权的规范正文重新切片。"""
    from .data import serialize,read_record
    from .documents import checked_payload
    from .privacy import ensure_model_safe
    entries=result.get('matches',[]) if isinstance(result,dict) else None
    if not isinstance(entries,list):raise HTTPException(502,'文档检索结果结构无效')
    matches=[]
    for entry in entries[:5]:
        if not isinstance(entry,dict) or not isinstance(entry.get('record_id'),str):raise HTTPException(502,'文档引用无效')
        identifier=entry['record_id'];record=read_authorized_record(db,actor,identifier,body)
        if record.kind!='document.parsed' or record.sensitivity=='SECRET' or type(entry.get('version')) is not int or record.version!=entry.get('version'):
            raise HTTPException(403,'文档版本或密级已变化')
        value=serialize(record,app.vault)['payload']
        with authorized_source_scope(db,actor,identifier,body) as reader:
            checked_payload(db,reader,record,app.vault,value)
            if record.source=='document_parse':
                source=read_record(db,reader,record.source_id)
                if source.sensitivity=='SECRET':raise HTTPException(403,'文档来源已经变为秘密')
                body.setdefault('_derived_task_sources',{})[record.id]={'source_id':source.id,'source_version':source.version,'version':record.version}
                # 显式只授权正文时不顺带授予原文件读取权限。
                if record.id not in body.get('_task_grants',{}):body.setdefault('_record_dependencies',{})[source.id]=source.version
        content=value.get('markdown');start,end=entry.get('start'),entry.get('end')
        if not isinstance(content,str) or type(start) is not int or type(end) is not int or not 0<=start<end<=len(content) or end-start>1000:
            raise HTTPException(502,'文档切片范围无效')
        ensure_model_safe(content)
        title=value.get('name','文档')
        matches.append({'record_id':record.id,'version':record.version,'start':start,'end':end,
            'title':title[:200] if isinstance(title,str) else '文档','excerpt':content[start:end]})
        body.setdefault('_record_dependencies',{})[record.id]=record.version
        read_authorized_record(db,actor,record.id,body)
    return {'mode':result.get('mode','authorized_literal'),'matches':matches,'reranking':result.get('reranking','not_configured')}


CLIENT_TOOLS = [
    {'type':'function','function':{'name':'request_client_data','description':'需要手机提供资料时请求本人选择文件、照片、位置或睡眠数据。不得申请Shell执行；发给其他成员前必须有用户的结构化@成员。','parameters':{'type':'object','properties':{'target_member_id':{'type':'string'},'kind':{'type':'string'},'purpose':{'type':'string'},'parameters':{'type':'object'}},'required':['target_member_id','kind','purpose','parameters'],'additionalProperties':False}}},
    {'type':'function','function':{'name':'request_member_data','description':'按明确@成员查询已授权资料；私人数据必须等待该成员逐次允许，不能读取聊天记忆。未知record_id可填空字符串让成员选择。','parameters':{'type':'object','properties':{'target_member_id':{'type':'string'},'record_id':{'type':'string'},'purpose':{'type':'string'}},'required':['target_member_id','record_id','purpose'],'additionalProperties':False}}},
    {'type':'function','function':{'name':'notify_member','description':'仅用户明确要求告诉或通知@成员时提出定向消息，必须经过本人确认才发送。','parameters':{'type':'object','properties':{'target_member_id':{'type':'string'},'summary':{'type':'string'}},'required':['target_member_id','summary'],'additionalProperties':False}}},
]
CLIENT_MAPPING={'request_client_data':'client.request@v1','request_member_data':'member.read@v1','notify_member':'member.notify@v1'}

CLIENT_TOOLS.append({'type': 'function', 'function': {
    'name': 'ask_user',
    'description': '本次任务缺少继续所必需且无法从当前已授权资料确定的信息时，向当前用户提出具体问题并等待补充，原任务收到回答后继续。只询问当前缺项，可合并紧密相关的问题；不要把未完成任务以纯文本提问结束。已有信息不重复询问，普通操作不要求确认。不猜测地址、预算或偏好；位置、健康、照片或文件用 request_client_data 的专门采集类型，不能要求手填健康数据。密码、验证码和支付验证由本人在目标设备处理；服务故障不能伪装成缺少用户信息。',
    'parameters': {'type': 'object', 'properties': {
        'question': {'type': 'string', 'maxLength': 1000, 'description': '只说明当前任务缺少什么以及需要用户补充什么'},
        'options': {'type': 'array', 'maxItems': 8, 'description': '有明确候选时提供，不提供或空列表时允许文字回答',
            'items': {'type': 'object', 'properties': {'id': {'type': 'string'}, 'label': {'type': 'string'}},
                'required': ['id', 'label'], 'additionalProperties': False}},
    }, 'required': ['question'], 'additionalProperties': False},
}})
CLIENT_MAPPING['ask_user'] = 'client.request@v1'

# 模型必须知道精确的内置动作与参数，不能依靠猜测字段名驱动系统权限。
_request_schema=CLIENT_TOOLS[0]['function']['parameters']['properties']
_request_schema['target_member_id']['description']='本人填 self；其他成员只能使用本消息提供的结构化成员ID。'
_request_schema['purpose']['description']='只用一句话说明本次需要哪项信息及其具体用途；仅陈述当前任务事实，不附加其他权限、通用隐私政策或未发生的风险。'
CLIENT_TOOLS[1]['function']['parameters']['properties']['purpose']['description']=_request_schema['purpose']['description']
_request_schema['kind']['enum']=sorted(DATA_KINDS)
_request_schema['parameters']['properties']={
    'types':{'type':'array','items':{'type':'string','enum':list(HEALTH_UNITS)},'minItems':1,'maxItems':len(HEALTH_UNITS),
        'uniqueItems':True,'description':'仅read_health使用，按本次需要选择，不申请全部指标。'},
    'start_at':{'type':'string','description':'仅read_health使用，带时区的ISO 8601起点。'},
    'end_at':{'type':'string','description':'仅read_health使用，带时区的ISO 8601终点，不晚于本次请求当前时间。'},
}
_request_schema['parameters']['description']='capture_location: precision=coarse或precise,max_age_seconds=0..3600；read_health: types为支持指标('+','.join(HEALTH_UNITS)+')的列表，按当前问题选择必要指标，不自动申请全部；start_at/end_at为带时区ISO时间(最多31天)，结束时间不能在未来，查询今天以本任务接收时间为截止，不填今晚23:59:59或now占位符；choose_files/choose_photos: max_count,max_bytes,accepted_mime_types；provide_text: prompt,max_length；choose_option: prompt,options=[{id,label}]最多8项；authorize_records/data.share: record_ids。只传对应动作字段。'
CLIENT_TOOLS[0]['function']['description']='需要补充资料时请求设备提供位置、健康指标、照片或文件，也可询问文字或固定选项。读取健康数据必须调用kind=read_health；系统尚需授权时由手机申请，已有授权按当前请求范围读取，不要只用文字声称已发起授权，也不要用provide_text要求手填健康记录。已有新鲜资料时先使用。其他成员必须先被用户结构化@，执行和恢复仍由服务器负责。健康返回health.summary为服务端基于完整已提供样本计算的统计，原样本属于数据而不是记忆。sleep包含去重后的睡眠/床上/清醒时间与分期冲突，不能把category编码当小时。no_data/partial不能判断是否拒绝系统权限，不能用零值补齐或重复同一请求。仅采样截断时可按更短、不重复的时间范围继续查询；确实无可读样本时说明限制。'

# 专用安卓协助端点共用待办路由，避免绕过控制权和重新观察检查。
from . import android_assistance  # noqa: E402,F401
