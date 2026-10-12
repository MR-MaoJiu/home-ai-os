"""按成员设置服务额度；与模型凭据独立保存，不扩大成员数据访问权。"""
import hashlib
from datetime import datetime, timezone
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, StrictInt
from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, Integer, String, select, func, text
from sqlalchemy.orm import Mapped, mapped_column
from .contracts import Contract
from .db import Base, Principal, now, scope
from .security import authenticate, owner
from .data import audit
from .quota_limits import TokenLimit, MAX_FINITE_TOKENS, UNLIMITED_TOKENS, shared_remaining

router = APIRouter(prefix='/api/v1/manage/member-budgets', tags=['成员额度'])


class MemberBudget(Base):
    __tablename__ = 'member_budgets'
    __table_args__ = (
        CheckConstraint(f'daily_tokens IS NULL OR daily_tokens BETWEEN 0 AND {MAX_FINITE_TOKENS}', name='member_token_limit'),
        CheckConstraint('NOT tokens_unlimited OR daily_tokens IS NULL', name='member_unlimited_token_shape'),
        CheckConstraint('daily_images IS NULL OR daily_images BETWEEN 0 AND 1000', name='member_image_limit'),
    )
    member_id: Mapped[str] = mapped_column(String, ForeignKey('principals.id', ondelete='CASCADE'), primary_key=True)
    household_id: Mapped[str] = mapped_column(String, index=True)
    daily_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tokens_unlimited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text('false'))
    daily_images: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[float] = mapped_column(default=now)


class MemberBudgetInput(Contract):
    daily_tokens: TokenLimit | None
    daily_images: Annotated[StrictInt, Field(ge=0, le=1000)] | None


def lock_budget_settings(db, household_id):
    """与云调用按 Token→图片顺序取锁，额度修改和预留不能交错覆盖。"""
    if db.get_bind().dialect.name == 'postgresql':
        for prefix in ('cloud-budget:', 'images:'):
            key = int.from_bytes(hashlib.sha256((prefix + household_id).encode()).digest()[:8], 'big', signed=True)
            db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})


def resolve_limits(app, db, actor, config=None):
    if config is None:
        from .model_routing import configuration
        config = configuration(app, db, actor.household_id)
    row = db.scalar(select(MemberBudget).where(MemberBudget.member_id == actor.user_id,
        MemberBudget.household_id == actor.household_id).execution_options(populate_existing=True))
    return {'daily_tokens':UNLIMITED_TOKENS if row and row.tokens_unlimited else row.daily_tokens if row and row.daily_tokens is not None else config.get('member_daily_tokens',0),
            'daily_images':row.daily_images if row and row.daily_images is not None else config.get('member_daily_images',0)}


def snapshot(app, db, actor):
    from .model_routing import configuration, ModelUsage
    from .image_generation import ImageUsage
    config = configuration(app, db, actor.household_id)
    day = datetime.now(timezone.utc).date().isoformat()
    def usage(model):
        return {identifier:int(amount) for identifier,amount in db.execute(select(model.owner_id, func.sum(model.charged))
            .where(model.household_id == actor.household_id, model.day == day).group_by(model.owner_id))}
    tokens, images = usage(ModelUsage), usage(ImageUsage)
    household = {'daily_tokens':config.get('household_daily_tokens',0), 'daily_images':config.get('household_daily_images',0),
                 'used_tokens':sum(tokens.values()), 'used_images':sum(images.values())}
    defaults = {'daily_tokens':config.get('member_daily_tokens',0), 'daily_images':config.get('member_daily_images',0)}
    rows = {row.member_id:row for row in db.scalars(select(MemberBudget).where(MemberBudget.household_id == actor.household_id))}
    members = []
    for person in db.scalars(select(Principal).where(Principal.household_id == actor.household_id).order_by(Principal.id)):
        row = rows.get(person.id)
        overrides = {'daily_tokens':UNLIMITED_TOKENS if row and row.tokens_unlimited else row.daily_tokens if row else None,
                     'daily_images':row.daily_images if row else None}
        limits = {key:value if value is not None else defaults[key] for key,value in overrides.items()}
        used = {'tokens':tokens.get(person.id,0), 'images':images.get(person.id,0)}
        remaining = {'tokens':shared_remaining(limits['daily_tokens'],used['tokens'],household['daily_tokens'],household['used_tokens']),
                     'images':max(0,min(limits['daily_images']-used['images'],household['daily_images']-household['used_images']))}
        members.append({'id':person.id,'name':person.name,'role':person.role,'overrides':overrides,
                        'limits':limits,'usage':used,'remaining':remaining})
    return {'day':day,'defaults':defaults,'household':household,'members':members}


@router.get('')
def read_member_budgets(request: Request, actor=Depends(authenticate)):
    owner(actor)
    with request.app.state.db() as db:
        scope(db, actor.user_id, actor.household_id)
        return snapshot(request.app.state, db, actor)


@router.put('/{member_id}')
def update_member_budget(member_id: str, body: MemberBudgetInput, request: Request, actor=Depends(authenticate)):
    owner(actor)
    app = request.app.state
    with app.db() as db:
        scope(db, actor.user_id, actor.household_id)
        lock_budget_settings(db, actor.household_id)
        person = db.scalar(select(Principal).where(Principal.id == member_id, Principal.household_id == actor.household_id))
        if not person: raise HTTPException(404, '家庭成员不存在')
        row = db.get(MemberBudget, member_id)
        previous = {'daily_tokens':UNLIMITED_TOKENS if row and row.tokens_unlimited else row.daily_tokens if row else None,
                    'daily_images':row.daily_images if row else None}
        if body.daily_tokens is None and body.daily_images is None:
            if row: db.delete(row)
        else:
            if row is None:
                row = MemberBudget(member_id=member_id, household_id=actor.household_id)
                db.add(row)
            row.tokens_unlimited = body.daily_tokens == UNLIMITED_TOKENS
            row.daily_tokens = None if row.tokens_unlimited else body.daily_tokens
            row.daily_images, row.updated_at = body.daily_images, now()
        audit(db, actor, 'member.budget_changed', member_id, {'previous':previous, 'current':body.model_dump()})
        db.commit()
        return next(member for member in snapshot(app, db, actor)['members'] if member['id'] == member_id)
