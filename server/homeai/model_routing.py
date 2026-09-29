"""模型用途、能力实测及家庭服务凭据。这里不改变业务数据查询身份。"""
import asyncio
import json
from datetime import datetime, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field
from sqlalchemy import String, Text, Integer, select
from sqlalchemy.orm import Mapped, mapped_column
from .contracts import Contract, ProviderManifest
from .crypto import canonical, digest
from .db import Base, Owned, Provider, Secret, now, scope, uid
from .security import authenticate, owner

router = APIRouter(prefix='/api/v1/manage/models', tags=['模型用途与验证'])
ROLES = ('local_fast', 'local_privacy', 'cloud_planner', 'vision')


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
    reserved: Mapped[int] = mapped_column(Integer)
    charged: Mapped[int] = mapped_column(Integer)
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
    vision: str | None = None
    shared_cloud: bool = False
    household_daily_tokens: int = Field(default=0, ge=0, le=100000000)
    member_daily_tokens: int = Field(default=0, ge=0, le=10000000)


def fingerprint(manifest):
    return digest(canonical(manifest.model_dump()))


def configuration(app, db, household):
    row = db.get(ModelConfiguration, household)
    return app.vault.open(row.configuration, household + ':model-configuration') if row else ConfigurationInput().model_dump()


def verification(db, household, manifest):
    row = db.get(ModelVerification, household + ':' + manifest.id)
    if row and row.fingerprint == fingerprint(manifest):
        return json.loads(row.report) | {'verified_at': row.verified_at}
    return {'text': False, 'tools': False, 'vision': False, 'privacy': False, 'status': 'configured', 'verified_at': None}


def resolve_role(app, db, actor, role):
    config = configuration(app, db, actor.household_id)
    identifier = config.get(role)
    if not identifier:
        if role in {'local_fast', 'local_privacy'}:
            manifest=app.registry.resolve(db, 'model.generate@v1', cloud=False)
            if role=='local_privacy' and not verification(db,actor.household_id,manifest).get('privacy'):
                raise HTTPException(503,'默认本地模型尚未通过隐私检测能力验证')
            return manifest
        raise HTTPException(503, '尚未配置' + ('云端规划模型' if role == 'cloud_planner' else '视觉模型'))
    row = db.get(Provider, identifier)
    if not row or not row.enabled:
        raise HTTPException(503, '配置的模型尚未启用')
    manifest = ProviderManifest.model_validate_json(row.manifest)
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
    if config.get('household_daily_tokens', 0) <= 0 or config.get('member_daily_tokens', 0) <= 0:
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
        return {'configuration': config, 'providers': [{'id': row.id, **verification(db, actor.household_id, ProviderManifest.model_validate_json(row.manifest))} for row in db.scalars(select(Provider)) if 'model.generate@v1' in ProviderManifest.model_validate_json(row.manifest).capabilities]}


@router.put('')
def update_configuration(body: ConfigurationInput, request: Request, actor=Depends(authenticate)):
    owner(actor)
    app = request.app.state
    with app.db() as db:
        scope(db, actor.user_id, actor.household_id)
        config = body.model_dump()
        for role in ROLES:
            if not config[role]:
                continue
            row = db.get(Provider, config[role])
            if not row:
                raise HTTPException(422, '指定模型不存在')
            manifest = ProviderManifest.model_validate_json(row.manifest)
            if 'model.generate@v1' not in manifest.capabilities or (role.startswith('local_') and manifest.cloud) or (role == 'cloud_planner' and not manifest.cloud):
                raise HTTPException(422, '模型类型与用途不匹配')
        if body.shared_cloud:
            if not body.cloud_planner or body.household_daily_tokens <= 0 or body.member_daily_tokens <= 0:
                raise HTTPException(422, '家庭共用需要云端模型、家庭和成员每日 Token 限额')
            config['_service_credentials'] = {}
            for provider_id in {body.cloud_planner, body.vision} - {None}:
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
            raise HTTPException(422, '该 Provider 不是兼容模型接口')
        from .cloud_gateway import probe
        report = await probe(app, db, actor, manifest)
        saved = db.get(ModelVerification, actor.household_id + ':' + provider_id)
        if saved is None:
            saved = ModelVerification(id=actor.household_id + ':' + provider_id, household_id=actor.household_id, provider_id=provider_id)
            db.add(saved)
        saved.fingerprint, saved.report, saved.verified_at = fingerprint(manifest), json.dumps(report), now()
        row.health = 'online' if report['text'] else 'unavailable'
        db.commit()
        return {**report, 'verified_at': saved.verified_at}


class BudgetExceeded(HTTPException):
    def __init__(self, detail):
        super().__init__(409, detail)


def reserve(app, db, actor, task, call_key, tokens):
    """按家庭配置行串行预留，进程中断仍收费预留，避免并发超额。"""
    from sqlalchemy import func
    if db.bind.dialect.name == 'postgresql':
        from sqlalchemy import text
        lock_key = int.from_bytes(__import__('hashlib').sha256(('cloud-budget:' + actor.household_id).encode()).digest()[:8], 'big', signed=True)
        db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock_key})
    row = db.get(ModelConfiguration, actor.household_id)
    config = configuration(app, db, actor.household_id)
    if row is None or not config.get('household_daily_tokens') or not config.get('member_daily_tokens'):
        raise BudgetExceeded('请先设置家庭和成员每日云模型 Token 预算')
    old = db.scalar(select(ModelUsage).where(ModelUsage.call_key == call_key))
    if old:
        raise BudgetExceeded('此云调用已有预留记录，不能自动重复发送')
    day = datetime.now(timezone.utc).date().isoformat()
    query = select(func.coalesce(func.sum(ModelUsage.charged), 0)).where(ModelUsage.household_id == actor.household_id, ModelUsage.day == day)
    total = db.scalar(query)
    member = db.scalar(query.where(ModelUsage.owner_id == actor.user_id))
    if total + tokens > config['household_daily_tokens'] or member + tokens > config['member_daily_tokens']:
        raise BudgetExceeded('已达到家庭或成员每日云模型 Token 预算')
    usage = ModelUsage(id=uid(), household_id=actor.household_id, owner_id=actor.user_id, task_id=task.id, call_key=call_key, day=day, reserved=tokens, charged=tokens)
    db.add(usage)
    db.flush()
    return usage
