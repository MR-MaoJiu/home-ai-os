"""成员额度使用隔离账本验证；真实PG并发用例需显式开启，绝不调用付费模型。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from types import SimpleNamespace
import os
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from conftest import SignedClient
from homeai.db import scope
from homeai.image_generation import ImageUsage, reserve_images
from homeai.model_routing import BudgetExceeded, ModelUsage, reserve, shared_key
from homeai.security import Actor
from test_cloud_gateway import setup_cloud

BASE = '/api/v1/manage/member-budgets'
MAX_SAFE_TOKEN_LIMIT = 9007199254740991


def defaults(admin, *, tokens=100, images=3, household_tokens=1000, household_images=10):
    old = admin.request('GET', '/api/v1/manage/models')
    assert old.status_code == 200, old.text
    config = old.json()['configuration']
    response = admin.request('PUT', '/api/v1/manage/models', {**config,
        'member_daily_tokens': tokens, 'member_daily_images': images,
        'household_daily_tokens': household_tokens, 'household_daily_images': household_images})
    assert response.status_code == 200, response.text


def listing(admin):
    response = admin.request('GET', BASE)
    assert response.status_code == 200, response.text
    return response.json()


def member(admin, identifier):
    return next(row for row in listing(admin)['members'] if row['id'] == identifier)


def override(admin, identifier, tokens=None, images=None):
    response = admin.request('PUT', BASE + '/' + identifier, {'daily_tokens': tokens, 'daily_images': images})
    assert response.status_code == 200, response.text
    return response.json()


def identity(user, household='h1', role='adult'):
    return Actor(user.user_id, household, user.device_id, role)


def add_tokens(db, user, amount, *, household='h1', day=None, status='SUCCEEDED'):
    row = ModelUsage(id=str(uuid.uuid4()), household_id=household, owner_id=user.user_id,
        task_id=str(uuid.uuid4()), call_key=str(uuid.uuid4()),
        day=day or datetime.now(timezone.utc).date().isoformat(), reserved=amount + 100,
        charged=amount, status=status)
    db.add(row)


def add_images(db, user, count=1, *, household='h1', day=None, status='SUCCEEDED'):
    for _ in range(count):
        db.add(ImageUsage(id=str(uuid.uuid4()), household_id=household, owner_id=user.user_id,
            task_id=str(uuid.uuid4()), call_key=str(uuid.uuid4()),
            day=day or datetime.now(timezone.utc).date().isoformat(), charged=1, status=status))


def reserve_token(system, user, amount, key=None):
    with system[2]() as db:
        actor = identity(user)
        scope(db, actor.user_id, actor.household_id)
        row = reserve(system[0].state, db, actor, SimpleNamespace(id=str(uuid.uuid4())), key or str(uuid.uuid4()), amount)
        db.commit()
        return row


def reserve_image(system, user, key=None):
    with system[2]() as db:
        actor = identity(user)
        scope(db, actor.user_id, actor.household_id)
        row = reserve_images(system[0].state, db, actor, str(uuid.uuid4()), key or str(uuid.uuid4()))
        db.commit()
        return row


def test_administrator_lists_only_own_household_and_rejects_forged_or_regular_member_access(system, alice):
    bob = SignedClient(system[1], system[2])
    outside = SignedClient(system[1], system[2], household='other-home', role='infrastructure_owner')
    defaults(alice)
    response = listing(alice)
    assert response['day'] == datetime.now(timezone.utc).date().isoformat()
    assert {row['id'] for row in response['members']} == {alice.user_id, bob.user_id}
    assert response['defaults'] == {'daily_tokens': 100, 'daily_images': 3}
    assert system[1].get(BASE).status_code == 401
    assert bob.request('GET', BASE).status_code == 403
    assert bob.request('PUT', BASE + '/' + bob.user_id, {'daily_tokens': 999, 'daily_images': 9}).status_code == 403
    assert alice.request('PUT', BASE + '/' + outside.user_id, {'daily_tokens': 1, 'daily_images': 1}).status_code == 404
    assert outside.request('PUT', BASE + '/' + bob.user_id, {'daily_tokens': 1, 'daily_images': 1}).status_code == 404
    assert alice.request('PUT', BASE + '/' + str(uuid.uuid4()), {'daily_tokens': 1, 'daily_images': 1}).status_code == 404
    assert alice.request('PUT', BASE + '/' + bob.user_id,
        {'daily_tokens': 10, 'daily_images': 1, 'household_id': 'other-home'}).status_code == 422


def test_independent_overrides_inherit_null_and_zero_blocks_only_selected_dimension(system, alice):
    bob = SignedClient(system[1], system[2])
    defaults(alice, tokens=40, images=3, household_tokens=200, household_images=10)
    initial = member(alice, bob.user_id)
    assert initial['overrides'] == {'daily_tokens': None, 'daily_images': None}
    assert initial['limits'] == {'daily_tokens': 40, 'daily_images': 3}
    changed = override(alice, bob.user_id, tokens=0, images=2)
    assert changed['limits'] == {'daily_tokens': 0, 'daily_images': 2}
    with pytest.raises(BudgetExceeded) as denied:
        reserve_token(system, bob, 1)
    assert denied.value.scope == 'member_daily' and denied.value.limit == 0
    reserve_image(system, bob)
    reserve_image(system, bob)
    with pytest.raises(BudgetExceeded):
        reserve_image(system, bob)
    changed = override(alice, bob.user_id, tokens=None, images=0)
    assert changed['limits'] == {'daily_tokens': 40, 'daily_images': 0}
    reserve_token(system, bob, 10)
    with pytest.raises(BudgetExceeded):
        reserve_image(system, bob)
    reset = override(alice, bob.user_id)
    assert reset['overrides'] == {'daily_tokens': None, 'daily_images': None}
    assert reset['limits'] == {'daily_tokens': 40, 'daily_images': 3}
    assert reset['usage'] == {'tokens': 10, 'images': 2}
    assert reset['remaining'] == {'tokens': 30, 'images': 1}
    assert member(alice, alice.user_id)['limits'] == {'daily_tokens': 40, 'daily_images': 3}


def test_remaining_counts_current_day_reservations_and_is_capped_by_household(system, alice):
    bob = SignedClient(system[1], system[2])
    outside = SignedClient(system[1], system[2], household='other-home')
    defaults(alice, tokens=100, images=3, household_tokens=200, household_images=5)
    override(alice, bob.user_id, tokens=300, images=5)
    with system[2]() as db:
        scope(db, alice.user_id, 'h1')
        add_tokens(db, alice, 40)
        add_tokens(db, alice, 60, status='RESERVED')
        add_tokens(db, bob, 40, status='UNKNOWN')
        add_tokens(db, bob, 999, day='2000-01-01')
        add_tokens(db, outside, 999, household='other-home')
        add_images(db, alice, 3, status='RESERVED')
        add_images(db, bob, 1, status='UNKNOWN')
        add_images(db, bob, 2, day='2000-01-01')
        add_images(db, outside, 2, household='other-home')
        db.commit()
    value = listing(alice)
    assert value['household'] == {'daily_tokens': 200, 'daily_images': 5, 'used_tokens': 140, 'used_images': 4}
    own, other = (next(row for row in value['members'] if row['id'] == user.user_id) for user in (alice, bob))
    assert own['usage'] == {'tokens': 100, 'images': 3}
    assert own['remaining'] == {'tokens': 0, 'images': 0}
    assert other['usage'] == {'tokens': 40, 'images': 1}
    assert other['remaining'] == {'tokens': 60, 'images': 1}
    assert 'call_key' not in str(value) and 'task_id' not in str(value)


def test_member_override_never_bypasses_household_token_or_image_limit(system, alice):
    bob = SignedClient(system[1], system[2])
    defaults(alice, tokens=100, images=5, household_tokens=100, household_images=2)
    override(alice, bob.user_id, tokens=500, images=20)
    reserve_token(system, alice, 60)
    with pytest.raises(BudgetExceeded) as denied:
        reserve_token(system, bob, 41)
    assert denied.value.scope == 'household_daily'
    reserve_token(system, bob, 40)
    reserve_image(system, alice)
    reserve_image(system, bob)
    with pytest.raises(BudgetExceeded):
        reserve_image(system, bob)
    assert member(alice, bob.user_id)['remaining'] == {'tokens': 0, 'images': 0}


def test_regular_model_save_keeps_overrides_and_limit_changes_never_clear_usage(system, alice):
    bob = SignedClient(system[1], system[2])
    charlie = SignedClient(system[1], system[2])
    defaults(alice)
    override(alice, bob.user_id, tokens=20, images=1)
    override(alice, charlie.user_id, tokens=None, images=4)
    reserve_token(system, bob, 12)
    reserve_image(system, bob)
    defaults(alice, tokens=60, images=2)
    bob_view = member(alice, bob.user_id)
    assert bob_view['overrides'] == {'daily_tokens': 20, 'daily_images': 1}
    assert bob_view['limits'] == {'daily_tokens': 20, 'daily_images': 1}
    assert bob_view['usage'] == {'tokens': 12, 'images': 1}
    assert member(alice, charlie.user_id)['limits'] == {'daily_tokens': 60, 'daily_images': 4}
    lowered = override(alice, bob.user_id, tokens=5, images=0)
    assert lowered['usage'] == {'tokens': 12, 'images': 1}
    assert lowered['remaining'] == {'tokens': 0, 'images': 0}
    raised = override(alice, bob.user_id, tokens=30, images=3)
    assert raised['usage'] == {'tokens': 12, 'images': 1}
    assert raised['remaining'] == {'tokens': 18, 'images': 2}


def test_shared_provider_with_zero_default_uses_explicit_member_limit_and_hides_secret(system, alice):
    manifest = setup_cloud(system, alice)
    bob = SignedClient(system[1], system[2])
    charlie = SignedClient(system[1], system[2])
    override(alice, bob.user_id, tokens=100, images=1)
    defaults(alice, tokens=0, images=0, household_tokens=1000, household_images=5)
    with system[2]() as db:
        scope(db, bob.user_id, 'h1')
        assert shared_key(system[0].state.vault, db, identity(bob), manifest) == 'service-secret'
    reserve_token(system, bob, 80)
    with pytest.raises(BudgetExceeded) as blocked:
        reserve_token(system, charlie, 1)
    assert blocked.value.scope == 'member_daily' and blocked.value.limit == 0
    response = alice.request('GET', BASE)
    assert response.status_code == 200
    assert 'service-secret' not in response.text and '_service_credentials' not in response.text


@pytest.mark.parametrize('body', [
    {'daily_tokens': -1, 'daily_images': None}, {'daily_tokens': 9007199254740992, 'daily_images': None},
    {'daily_tokens': None, 'daily_images': -1}, {'daily_tokens': None, 'daily_images': 1001},
    {'daily_tokens': True, 'daily_images': None}, {'daily_tokens': None, 'daily_images': '2'},
    {'daily_tokens': 'Infinity', 'daily_images': None}, {'daily_tokens': None, 'daily_images': 'unlimited'},
    {'daily_tokens': 10, 'daily_images': 1, 'owner_id': 'forged'},
])
def test_invalid_override_contract_does_not_change_effective_limits(system, alice, body):
    bob = SignedClient(system[1], system[2])
    defaults(alice)
    before = member(alice, bob.user_id)
    assert alice.request('PUT', BASE + '/' + bob.user_id, body).status_code == 422
    after = member(alice, bob.user_id)
    assert after['overrides'] == before['overrides'] and after['limits'] == before['limits']


@pytest.mark.parametrize('field', ['household_daily_tokens', 'member_daily_tokens'])
@pytest.mark.parametrize('limit', [0, MAX_SAFE_TOKEN_LIMIT, 'unlimited'])
def test_model_default_token_limits_accept_finite_bounds_and_unlimited(system, alice, field, limit):
    defaults(alice)
    config = alice.request('GET', '/api/v1/manage/models').json()['configuration']
    response = alice.request('PUT', '/api/v1/manage/models', {**config, field: limit})
    assert response.status_code == 200, response.text
    current = alice.request('GET', '/api/v1/manage/models').json()['configuration']
    assert current[field] == limit


@pytest.mark.parametrize('field', ['household_daily_tokens', 'member_daily_tokens'])
def test_model_default_token_limits_reject_above_safe_integer_without_saving(system, alice, field):
    defaults(alice)
    config = alice.request('GET', '/api/v1/manage/models').json()['configuration']
    response = alice.request('PUT', '/api/v1/manage/models', {**config, field: MAX_SAFE_TOKEN_LIMIT + 1})
    assert response.status_code == 422
    current = alice.request('GET', '/api/v1/manage/models').json()['configuration']
    assert current == config


@pytest.mark.parametrize('limit', [0, MAX_SAFE_TOKEN_LIMIT, 'unlimited'])
def test_member_token_limit_accepts_finite_or_unlimited_and_null_inherits(system, alice, limit):
    bob = SignedClient(system[1], system[2])
    defaults(alice, tokens=MAX_SAFE_TOKEN_LIMIT, household_tokens='unlimited')
    saved = override(alice, bob.user_id, tokens=limit)
    assert saved['overrides']['daily_tokens'] == limit
    assert saved['limits']['daily_tokens'] == limit
    inherited = override(alice, bob.user_id, tokens=None)
    assert inherited['overrides']['daily_tokens'] is None
    assert inherited['limits']['daily_tokens'] == MAX_SAFE_TOKEN_LIMIT


def test_reservations_remain_exact_at_safe_integer_boundary_without_overflow_or_refund(system, alice):
    bob = SignedClient(system[1], system[2])
    ceiling = MAX_SAFE_TOKEN_LIMIT
    defaults(alice, tokens=ceiling, household_tokens=ceiling)
    override(alice, bob.user_id, tokens=ceiling)
    with system[2]() as db:
        scope(db, bob.user_id, 'h1')
        # 预置历史占用验证整数累计；本次各次预留仍不超过64K，不提高单任务预算。
        add_tokens(db, bob, ceiling - 65536, status='UNKNOWN')
        db.commit()
    reserve_token(system, bob, 65535)
    assert member(alice, bob.user_id)['remaining']['tokens'] == 1
    reserve_token(system, bob, 1)
    exact = member(alice, bob.user_id)
    assert exact['usage']['tokens'] == ceiling and exact['remaining']['tokens'] == 0
    with pytest.raises(BudgetExceeded) as denied:
        reserve_token(system, bob, 1)
    assert denied.value.used == ceiling and denied.value.requested == 1 and denied.value.limit == ceiling
    assert member(alice, bob.user_id)['usage']['tokens'] == ceiling


@pytest.mark.parametrize('household_limit,member_limit,expected_scope', [
    (100, 'unlimited', 'household_daily'), ('unlimited', 100, 'member_daily'),
])
def test_unlimited_one_side_never_bypasses_other_finite_limit(system, alice, household_limit, member_limit, expected_scope):
    bob = SignedClient(system[1], system[2])
    defaults(alice, tokens=member_limit, household_tokens=household_limit)
    reserve_token(system, bob, 99)
    assert member(alice, bob.user_id)['remaining']['tokens'] == 1
    reserve_token(system, bob, 1)
    with pytest.raises(BudgetExceeded) as denied:
        reserve_token(system, bob, 1)
    assert denied.value.scope == expected_scope and denied.value.limit == 100
    assert member(alice, bob.user_id)['remaining']['tokens'] == 0


@pytest.mark.parametrize('household_limit,member_limit,expected_scope', [
    (0, 'unlimited', 'household_daily'), ('unlimited', 0, 'member_daily'),
])
def test_zero_token_limit_still_disables_when_other_side_is_unlimited(system, alice, household_limit, member_limit, expected_scope):
    bob = SignedClient(system[1], system[2])
    defaults(alice, tokens=member_limit, household_tokens=household_limit)
    with pytest.raises(BudgetExceeded) as denied:
        reserve_token(system, bob, 1)
    assert denied.value.scope == expected_scope and denied.value.limit == 0
    assert member(alice, bob.user_id)['remaining']['tokens'] == 0


def test_explicit_unlimited_is_distinct_from_null_inheritance_and_zero(system, alice):
    bob = SignedClient(system[1], system[2])
    defaults(alice, tokens=20, household_tokens='unlimited')
    explicit = override(alice, bob.user_id, tokens='unlimited')
    assert explicit['overrides']['daily_tokens'] == 'unlimited'
    assert explicit['limits']['daily_tokens'] == 'unlimited' and explicit['remaining']['tokens'] is None
    reserve_token(system, bob, 30)
    defaults(alice, tokens=10, household_tokens='unlimited')
    assert member(alice, bob.user_id)['limits']['daily_tokens'] == 'unlimited'
    assert member(alice, alice.user_id)['limits']['daily_tokens'] == 10
    finite = override(alice, bob.user_id, tokens=15)
    assert finite['overrides']['daily_tokens'] == 15 and finite['remaining']['tokens'] == 0
    inherited = override(alice, bob.user_id, tokens=None)
    assert inherited['overrides']['daily_tokens'] is None and inherited['limits']['daily_tokens'] == 10
    defaults(alice, tokens='unlimited', household_tokens='unlimited')
    assert member(alice, bob.user_id)['remaining']['tokens'] is None
    blocked = override(alice, bob.user_id, tokens=0)
    assert blocked['limits']['daily_tokens'] == 0 and blocked['remaining']['tokens'] == 0
    assert blocked['usage']['tokens'] == 30
    with pytest.raises(BudgetExceeded):
        reserve_token(system, bob, 1)


def test_unlimited_json_has_no_infinity_and_preserves_image_limits_and_real_usage(system, alice):
    bob = SignedClient(system[1], system[2])
    defaults(alice, tokens='unlimited', household_tokens='unlimited', images=2, household_images=4)
    reserve_token(system, bob, 65536)
    result = alice.request('GET', BASE)
    assert result.status_code == 200
    assert 'Infinity' not in result.text and 'NaN' not in result.text
    value = result.json()
    assert value['defaults']['daily_tokens'] == 'unlimited'
    assert value['household']['daily_tokens'] == 'unlimited' and value['household']['used_tokens'] == 65536
    target = next(row for row in value['members'] if row['id'] == bob.user_id)
    assert target['remaining'] == {'tokens': None, 'images': 2}
    assert target['usage']['tokens'] == 65536
    assert target['limits']['daily_images'] == 2 and value['household']['daily_images'] == 4


def test_shared_model_credentials_work_with_unlimited_token_defaults(system, alice):
    manifest = setup_cloud(system, alice)
    bob = SignedClient(system[1], system[2])
    defaults(alice, tokens='unlimited', household_tokens='unlimited')
    with system[2]() as db:
        scope(db, bob.user_id, 'h1')
        assert shared_key(system[0].state.vault, db, identity(bob), manifest) == 'service-secret'
    reserve_token(system, bob, 64)
    assert member(alice, bob.user_id)['remaining']['tokens'] is None


def test_existing_regular_numeric_configuration_is_not_replaced_by_unlimited(system, alice):
    bob = SignedClient(system[1], system[2])
    defaults(alice, tokens=6789, household_tokens=12345)
    override(alice, bob.user_id, tokens=321, images=2)
    for _ in range(2):
        config = alice.request('GET', '/api/v1/manage/models').json()['configuration']
        assert type(config['household_daily_tokens']) is int and config['household_daily_tokens'] == 12345
        assert type(config['member_daily_tokens']) is int and config['member_daily_tokens'] == 6789
        target = member(alice, bob.user_id)
        assert type(target['overrides']['daily_tokens']) is int and target['limits']['daily_tokens'] == 321


@pytest.mark.skipif(os.getenv('HOMEAI_MEMBER_BUDGETS_LIVE') != '1', reason='需显式开启隔离homeai_test PostgreSQL并发验证')
@pytest.mark.parametrize('dimension', ['tokens', 'images'])
def test_postgresql_concurrent_reservations_cannot_exceed_household_budget(tmp_path, dimension):
    from homeai.api import create_app
    from homeai.config import Settings
    from homeai.crypto import Vault
    settings = Settings()
    settings.database_url = settings.database_url.rsplit('/', 1)[0] + '/homeai_test'
    settings.state_dir, settings.direct_enabled = tmp_path, False
    app = create_app(settings, vault=Vault(os.urandom(32)))
    client = TestClient(app)
    household = 'qa-member-budget-' + uuid.uuid4().hex
    admin = SignedClient(client, app.state.db, household=household, role='infrastructure_owner')
    bob = SignedClient(client, app.state.db, household=household)
    try:
        defaults(admin, tokens=10, images=1, household_tokens=10, household_images=1)
        override(admin, bob.user_id, tokens=100, images=10)
        barrier = Barrier(2)
        def compete(user):
            actor = identity(user, household)
            with app.state.db() as db:
                scope(db, actor.user_id, actor.household_id)
                barrier.wait(timeout=10)
                try:
                    if dimension == 'tokens': reserve(app.state, db, actor, SimpleNamespace(id=str(uuid.uuid4())), str(uuid.uuid4()), 7)
                    else: reserve_images(app.state, db, actor, str(uuid.uuid4()), str(uuid.uuid4()))
                    db.commit()
                    return 'accepted'
                except BudgetExceeded as error:
                    assert type(error.used) is int
                    json.dumps(error.metadata)
                    db.rollback()
                    return 'blocked'
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(compete, (admin, bob)))
        assert sorted(results) == ['accepted', 'blocked']
        with app.state.db() as db:
            scope(db, admin.user_id, household)
            model = ModelUsage if dimension == 'tokens' else ImageUsage
            assert db.scalar(select(func.sum(model.charged)).where(model.household_id == household)) == (7 if dimension == 'tokens' else 1)
    finally:
        for user in (admin, bob):
            user.request('DELETE', '/api/v1/devices/' + user.device_id)
        client.close()
        app.state.db.kw['bind'].dispose()


@pytest.mark.skipif(os.getenv('HOMEAI_MEMBER_BUDGETS_LIVE') != '1', reason='需显式开启隔离homeai_test PostgreSQL RLS验证')
def test_postgresql_member_budget_rls_rejects_forged_household_and_member_writes(tmp_path):
    from homeai.api import create_app
    from homeai.config import Settings
    from homeai.crypto import Vault
    from homeai.member_budgets import MemberBudget
    settings = Settings()
    settings.database_url = settings.database_url.rsplit('/', 1)[0] + '/homeai_test'
    settings.state_dir, settings.direct_enabled = tmp_path, False
    app = create_app(settings, vault=Vault(os.urandom(32)))
    client = TestClient(app)
    household, other_household = 'qa-budget-rls-' + uuid.uuid4().hex, 'qa-budget-other-' + uuid.uuid4().hex
    admin = SignedClient(client, app.state.db, household=household, role='infrastructure_owner')
    bob = SignedClient(client, app.state.db, household=household)
    charlie = SignedClient(client, app.state.db, household=household)
    outside = SignedClient(client, app.state.db, household=other_household, role='infrastructure_owner')
    try:
        defaults(admin)
        defaults(outside)
        override(admin, admin.user_id, tokens=80, images=2)
        override(admin, bob.user_id, tokens=40, images=1)
        override(outside, outside.user_id, tokens=90, images=4)
        with app.state.db() as db:
            flags = db.execute(text('SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user')).one()
            assert not flags.rolsuper and not flags.rolbypassrls
            scope(db, bob.user_id, household)
            assert {row.member_id for row in db.scalars(select(MemberBudget))} == {bob.user_id}
            denied = db.execute(update(MemberBudget).where(MemberBudget.member_id == bob.user_id).values(daily_tokens=999))
            assert denied.rowcount == 0
            db.rollback()
        # 普通成员不能写额度，即便目标就是同家庭自己。
        with app.state.db() as db:
            scope(db, charlie.user_id, household)
            db.add(MemberBudget(member_id=charlie.user_id, household_id=household, daily_tokens=999, daily_images=9))
            with pytest.raises(DBAPIError) as denied:
                db.flush()
            assert denied.value.orig.sqlstate == '42501'
            db.rollback()
        # 家庭管理员也不能伪造外家庭成员的归属来插入额度记录。
        with app.state.db() as db:
            scope(db, admin.user_id, household)
            assert db.get(MemberBudget, outside.user_id) is None
            db.add(MemberBudget(member_id=outside.user_id, household_id=household, daily_tokens=999, daily_images=9))
            with pytest.raises(DBAPIError) as denied:
                db.flush()
            assert denied.value.orig.sqlstate == '42501'
            db.rollback()
        # 只改连接上下文中的家庭ID，不能把本家庭管理员变成另一个家庭管理员。
        with app.state.db() as db:
            scope(db, admin.user_id, other_household)
            assert list(db.scalars(select(MemberBudget))) == []
        assert member(admin, bob.user_id)['limits'] == {'daily_tokens': 40, 'daily_images': 1}
        assert member(outside, outside.user_id)['limits'] == {'daily_tokens': 90, 'daily_images': 4}
    finally:
        for user in (admin, bob, charlie, outside):
            user.request('DELETE', '/api/v1/devices/' + user.device_id)
        client.close()
        app.state.db.kw['bind'].dispose()


@pytest.mark.skipif(os.getenv('HOMEAI_MEMBER_BUDGETS_LIVE') != '1', reason='需homeai_test迁移0024后显式验证unlimited及BigInteger额度约束')
def test_postgresql_unlimited_and_safe_integer_token_constraints(tmp_path):
    from homeai.api import create_app
    from homeai.config import Settings
    from homeai.crypto import Vault
    from homeai.member_budgets import MemberBudget
    settings = Settings()
    settings.database_url = settings.database_url.rsplit('/', 1)[0] + '/homeai_test'
    settings.state_dir, settings.direct_enabled = tmp_path, False
    app = create_app(settings, vault=Vault(os.urandom(32)))
    client = TestClient(app)
    household = 'qa-budget-unlimited-' + uuid.uuid4().hex
    admin = SignedClient(client, app.state.db, household=household, role='infrastructure_owner')
    bob = SignedClient(client, app.state.db, household=household)
    try:
        defaults(admin, tokens='unlimited', household_tokens='unlimited')
        override(admin, bob.user_id, tokens=0)
        with app.state.db() as db:
            scope(db, admin.user_id, household)
            db.get(MemberBudget, bob.user_id).daily_tokens = MAX_SAFE_TOKEN_LIMIT
            db.commit()
        assert member(admin, bob.user_id)['limits']['daily_tokens'] == MAX_SAFE_TOKEN_LIMIT
        with app.state.db() as db:
            scope(db, admin.user_id, household)
            db.get(MemberBudget, bob.user_id).daily_tokens = MAX_SAFE_TOKEN_LIMIT + 1
            with pytest.raises(DBAPIError) as rejected:
                db.flush()
            assert rejected.value.orig.sqlstate == '23514'
            assert rejected.value.orig.diag.constraint_name == 'member_token_limit'
            db.rollback()
        assert member(admin, bob.user_id)['limits']['daily_tokens'] == MAX_SAFE_TOKEN_LIMIT
        explicit = override(admin, bob.user_id, tokens='unlimited')
        assert explicit['overrides']['daily_tokens'] == 'unlimited' and explicit['remaining']['tokens'] is None
        with app.state.db() as db:
            scope(db, bob.user_id, household)
            row = db.get(MemberBudget, bob.user_id)
            assert row.tokens_unlimited is True and row.daily_tokens is None
            add_tokens(db, bob, MAX_SAFE_TOKEN_LIMIT - 65536, household=household, status='UNKNOWN')
            db.commit()
            reserve(app.state, db, identity(bob, household), SimpleNamespace(id=str(uuid.uuid4())), str(uuid.uuid4()), 65536)
            db.commit()
        assert member(admin, bob.user_id)['usage']['tokens'] == MAX_SAFE_TOKEN_LIMIT
        inherited = override(admin, bob.user_id, tokens=None)
        assert inherited['overrides']['daily_tokens'] is None and inherited['limits']['daily_tokens'] == 'unlimited'
        assert inherited['usage']['tokens'] == MAX_SAFE_TOKEN_LIMIT
    finally:
        for user in (admin, bob):
            response = user.request('DELETE', '/api/v1/devices/' + user.device_id)
            assert response.status_code == 200, response.text
        client.close()
        app.state.db.kw['bind'].dispose()
