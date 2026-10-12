"""模型用途、能力实测及家庭服务凭据。这里不改变业务数据查询身份。"""
import asyncio
import json
from datetime import datetime, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, model_validator
from sqlalchemy import String, Text, BigInteger, select
from sqlalchemy.orm import Mapped, mapped_column
from .contracts import Contract, ProviderManifest
from .crypto import canonical, digest
from .db import Base, Owned, Provider, Secret, now, scope, uid
from .security import authenticate, owner
from .quota_limits import TokenLimit, UNLIMITED_TOKENS

router = APIRouter(prefix='/api/v1/manage/models', tags=['模型用途与验证'])
ROLES = ('local_fast', 'local_privacy', 'cloud_planner', 'vision', 'image_generation', 'image_editing')
IMAGE_CAPABILITIES = {'image_generation': 'image.generate@v1', 'image_editing': 'image.edit@v1'}
MODEL_CAPABILITIES = {'model.generate@v1', *IMAGE_CAPABILITIES.values()}


class ModelConfiguration(Base):
    __tablename__ = 'model_configurations'
    household_id: Mapped[str] = mapped_column(String, primary_key=True)
    configuration: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[float] = mapped_column(default=now)


class ModelVerification(Base):
    __tablename__ = 'model_verifications'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    household_id: Mapped[str] = mapped_column(String, index=True)
    provider_id: Mapped[str] = mapped_column(String)
    fingerprint: Mapped[str] = mapped_column(String)
    report: Mapped[str] = mapped_column(Text)
    verified_at: Mapped[float] = mapped_column(default=now)


class ModelUsage(Owned, Base):
    __tablename__ = 'model_usage'
    task_id: Mapped[str] = mapped_column(String)
    call_key: Mapped[str] = mapped_column(String, unique=True)
    day: Mapped[str] = mapped_column(String)
    reserved: Mapped[int] = mapped_column(BigInteger)
    charged: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String, default='RESERVED')


class RedactionArtifact(Owned, Base):
    __tablename__ = 'redaction_artifacts'
    task_id: Mapped[str] = mapped_column(String, index=True)
    payload: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[float]


class ConfigurationInput(Contract):
    local_fast: str | None = None
    local_privacy: str | None = None
    cloud_planner: str | None = None
    cloud_planner_thinking: Literal['provider_default', 'disabled'] = 'provider_default'
    vision: str | None = None
    image_generation: str | None = None
    image_editing: str | None = None
    shared_cloud: bool = False
    household_daily_tokens: TokenLimit = 0
    member_daily_tokens: TokenLimit = 0
    household_daily_images: int = Field(default=0, ge=0, le=10000)
    member_daily_images: int = Field(default=0, ge=0, le=1000)


class ImageVerificationInput(Contract):
    generate: bool = False
    edit: bool = False
    confirm_usage: Literal[True]

    @model_validator(mode='after')
    def selected(self):
        if not (self.generate or self.edit):
            raise ValueError('至少选择一项图片能力进行实测')
        return self


def fingerprint(manifest):
    return digest(canonical(manifest.model_dump()))


def configuration(app, db, household):
    row = db.get(ModelConfiguration, household, populate_existing=True)
    defaults = ConfigurationInput().model_dump()
    return defaults | app.vault.open(row.configuration, household + ':model-configuration') if row else defaults


def verification(db, household, manifest):
    defaults = {'text': False, 'tools': False, 'vision': False, 'privacy': False,
        'image_generation': False, 'image_editing': False, 'status': 'configured', 'verified_at': None}
    row = db.get(ModelVerification, household + ':' + manifest.id)
    if row and row.fingerprint == fingerprint(manifest):
        return defaults | json.loads(row.report) | {'verified_at': row.verified_at}
    return defaults


def resolve_role(app, db, actor, role):
    if role not in ROLES:
        raise HTTPException(422, '未知模型用途')
    config = configuration(app, db, actor.household_id)
    identifier = config.get(role)
    if not identifier:
        if role in {'local_fast', 'local_privacy'}:
            manifest=app.registry.resolve(db, 'model.generate@v1', cloud=False)
            if role=='local_privacy' and not verification(db,actor.household_id,manifest).get('privacy'):
                raise HTTPException(503,'默认本地模型尚未通过隐私检测能力验证')
            return manifest
        raise HTTPException(503, '尚未配置' + {'cloud_planner': '云端规划模型', 'vision': '视觉模型',
            'image_generation': '图片生成模型', 'image_editing': '图片编辑模型'}[role])
    row = db.get(Provider, identifier)
    if not row or not row.enabled:
        raise HTTPException(503, '配置的模型尚未启用')
    manifest = ProviderManifest.model_validate_json(row.manifest)
    if role in IMAGE_CAPABILITIES:
        if manifest.adapter != 'openai' or IMAGE_CAPABILITIES[role] not in manifest.capabilities:
            raise HTTPException(422, '图片模型类型与用途不匹配')
        # 图片用途由明确配置启用；是否实测通过仍单独展示，不能用视觉验证替代。
        return manifest
    report = verification(db, actor.household_id, manifest)
    required = 'vision' if role == 'vision' else 'tools' if role == 'cloud_planner' else 'privacy' if role == 'local_privacy' else 'text'
    if not report.get(required) or (role == 'cloud_planner' and not report.get('text')):
        raise HTTPException(503, '模型用途尚未通过能力验证，请在模型设置中验证')
    if role.startswith('local_') and manifest.cloud:
        raise HTTPException(403, '隐私处理和本地简答不能使用云模型')
    if role == 'cloud_planner' and not manifest.cloud:
        raise HTTPException(422, '云端规划用途必须配置云模型')
    return manifest


def shared_key(vault, db, actor, manifest):
    """只解封已由管理员单独授权的模型服务凭据，不切换为管理员主体。"""
    row = db.get(ModelConfiguration, actor.household_id)
    if not row:
        return None
    config = vault.open(row.configuration, actor.household_id + ':model-configuration')
    grant = config.get('_service_credentials', {}).get(manifest.id) or config.get('_service_credential')
    if not config.get('shared_cloud') or not grant or not manifest.cloud or grant['fingerprint'] != fingerprint(manifest):
        return None
    if config.get('household_daily_tokens', 0) == 0:
        return None
    return grant['key']


@router.get('')
def read_configuration(request: Request, actor=Depends(authenticate)):
    owner(actor)
    with request.app.state.db() as db:
        scope(db, actor.user_id, actor.household_id)
        config = configuration(request.app.state, db, actor.household_id)
        config.pop('_service_credential', None)
        config.pop('_service_credentials', None)
        return {'configuration': config, 'providers': [{'id': row.id, **verification(db, actor.household_id, ProviderManifest.model_validate_json(row.manifest))} for row in db.scalars(select(Provider)) if MODEL_CAPABILITIES.intersection(ProviderManifest.model_validate_json(row.manifest).capabilities)]}


@router.put('')
def update_configuration(body: ConfigurationInput, request: Request, actor=Depends(authenticate)):
    owner(actor)
    app = request.app.state
    with app.db() as db:
        scope(db, actor.user_id, actor.household_id)
        from .member_budgets import lock_budget_settings
        lock_budget_settings(db, actor.household_id)
        config = body.model_dump()
        for role in ROLES:
            if not config[role]:
                continue
            row = db.get(Provider, config[role])
            if not row:
                raise HTTPException(422, '指定模型不存在')
            manifest = ProviderManifest.model_validate_json(row.manifest)
            capability = IMAGE_CAPABILITIES.get(role, 'model.generate@v1')
            if capability not in manifest.capabilities or (role in IMAGE_CAPABILITIES and manifest.adapter != 'openai') or (role.startswith('local_') and manifest.cloud) or (role == 'cloud_planner' and not manifest.cloud):
                raise HTTPException(422, '模型类型与用途不匹配')
        if body.shared_cloud:
            if not body.cloud_planner or body.household_daily_tokens == 0:
                raise HTTPException(422, '家庭共用需要云端模型和家庭每日 Token 限额；成员默认额度可为 0，再逐人开放')
            config['_service_credentials'] = {}
            for provider_id in {body.cloud_planner, body.vision, body.image_generation, body.image_editing} - {None}:
                manifest = ProviderManifest.model_validate_json(db.get(Provider, provider_id).manifest)
                if not manifest.cloud:
                    continue
                secret = db.get(Secret, manifest.secret_id) if manifest.secret_id else None
                if not secret or secret.owner_id != actor.user_id or secret.provider_id != manifest.id:
                    raise HTTPException(403, '只能授权本人配置的云服务凭据')
                config['_service_credentials'][manifest.id] = {'fingerprint': fingerprint(manifest), 'key': app.vault.open(secret.value, actor.user_id + ':secret:' + secret.id)}
        row = db.get(ModelConfiguration, actor.household_id)
        if row is None:
            row = ModelConfiguration(household_id=actor.household_id)
            db.add(row)
        row.configuration = app.vault.seal(config, actor.household_id + ':model-configuration')
        row.updated_at = now()
        from .data import audit
        audit(db, actor, 'model.configuration', actor.household_id)
        db.commit()
        return {'status': 'saved'}


def save_verification(db, household, manifest, report, capabilities):
    """仅更新本次实际探测的能力，保留同一配置此前已完成的其他实测。"""
    merged = verification(db, household, manifest)
    merged.pop('verified_at', None)
    errors = dict(merged.get('errors', {}))
    for capability in capabilities:
        merged[capability] = report.get(capability) is True
        errors.pop(capability, None)
        if capability in report.get('errors', {}):
            errors[capability] = report['errors'][capability]
    merged['errors'] = errors
    if report.get('resolved_model'):
        merged['resolved_model'] = report['resolved_model']
    merged['status'] = 'verified' if any(merged.get(key) for key in ('text', *IMAGE_CAPABILITIES)) else 'unavailable'
    saved = db.get(ModelVerification, household + ':' + manifest.id)
    if saved is None:
        saved = ModelVerification(id=household + ':' + manifest.id, household_id=household, provider_id=manifest.id)
        db.add(saved)
    saved.fingerprint, saved.report, saved.verified_at = fingerprint(manifest), json.dumps(merged), now()
    return merged | {'verified_at': saved.verified_at}


@router.post('/{provider_id}/verify')
async def verify_provider(provider_id: str, request: Request, actor=Depends(authenticate)):
    owner(actor)
    app = request.app.state
    with app.db() as db:
        scope(db, actor.user_id, actor.household_id)
        row = db.get(Provider, provider_id)
        if not row:
            raise HTTPException(404, '模型不存在')
        manifest = ProviderManifest.model_validate_json(row.manifest)
        if 'model.generate@v1' not in manifest.capabilities or manifest.adapter != 'openai':
            raise HTTPException(422, '该 Provider 未配置文本接口；图片模型请使用独立的图片能力验证')
        from .cloud_gateway import probe
        report = await probe(app, db, actor, manifest)
        report = save_verification(db, actor.household_id, manifest, report, ('text', 'tools', 'vision', 'privacy'))
        row.health = 'online' if report['status'] == 'verified' else 'unavailable'
        db.commit()
        return report


@router.post('/{provider_id}/verify-images')
async def verify_image_provider(provider_id: str, body: ImageVerificationInput, request: Request, actor=Depends(authenticate)):
    owner(actor)
    app = request.app.state
    with app.db() as db:
        scope(db, actor.user_id, actor.household_id)
        row = db.get(Provider, provider_id)
        if not row:
            raise HTTPException(404, '模型不存在')
        manifest = ProviderManifest.model_validate_json(row.manifest)
        selected = [role for role, requested in (('image_generation', body.generate), ('image_editing', body.edit)) if requested]
        if manifest.adapter != 'openai' or any(IMAGE_CAPABILITIES[role] not in manifest.capabilities for role in selected):
            raise HTTPException(422, '所选图片能力未配置，不能调用其他用途的模型接口')
        from .image_generation import probe
        result = await probe(app, db, actor, manifest, generate=body.generate, edit=body.edit)
        report = save_verification(db, actor.household_id, manifest, result, selected)
        row.health = 'online' if report['status'] == 'verified' else 'unavailable'
        db.commit()
        return report


class BudgetExceeded(HTTPException):
    def __init__(self, detail, *, code='budget_exceeded', scope='task', used=None,
                 requested=None, limit=None, recoverable=True):
        super().__init__(409, detail)
        self.code, self.scope = code, scope
        self.used, self.requested, self.limit = used, requested, limit
        self.recoverable = recoverable

    @property
    def metadata(self):
        return {'code': self.code, 'scope': self.scope, 'used': self.used,
                'requested': self.requested, 'limit': self.limit, 'recoverable': self.recoverable}


def reserve(app, db, actor, task, call_key, tokens):
    """串行预留额度；未结算请求保留占用，不把估算当作服务商实际用量。"""
    from sqlalchemy import func
    if db.bind.dialect.name == 'postgresql':
        from sqlalchemy import text
        lock_key = int.from_bytes(__import__('hashlib').sha256(('cloud-budget:' + actor.household_id).encode()).digest()[:8], 'big', signed=True)
        db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock_key})
    old = db.scalar(select(ModelUsage).where(ModelUsage.call_key == call_key))
    if old:
        raise HTTPException(409, '此云调用已有费用预留或派发记录，不能再次发送；请先核对原调用结果，调整额度不能解除此限制')
    config = configuration(app, db, actor.household_id)
    from .member_budgets import resolve_limits
    limits = resolve_limits(app, db, actor, config)
    day = datetime.now(timezone.utc).date().isoformat()
    query = select(func.coalesce(func.sum(ModelUsage.charged), 0)).where(ModelUsage.household_id == actor.household_id, ModelUsage.day == day)
    total = int(db.scalar(query) or 0)
    member = int(db.scalar(query.where(ModelUsage.owner_id == actor.user_id)) or 0)
    for label, budget_scope, used, limit in (
        ('家庭', 'household_daily', total, config.get('household_daily_tokens', 0)),
        ('本人', 'member_daily', member, limits['daily_tokens']),
    ):
        if limit == UNLIMITED_TOKENS:
            continue
        if not limit or used + tokens > limit:
            message = f'{label}每日云模型 Token 额度' + ('尚未配置' if not limit else '不足')
            message += f'：已占用 {used}，下次需预留 {tokens}，上限 {limit}。占用包含尚未结算的预留，不等于服务商实际用量。'
            code = 'daily_budget_not_configured' if not limit else budget_scope + '_budget_exceeded'
            raise BudgetExceeded(message, code=code, scope=budget_scope, used=used,
                                 requested=tokens, limit=limit, recoverable=True)
    usage = ModelUsage(id=uid(), household_id=actor.household_id, owner_id=actor.user_id, task_id=task.id, call_key=call_key, day=day, reserved=tokens, charged=tokens)
    db.add(usage)
    db.flush()
    return usage
