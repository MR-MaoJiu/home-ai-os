"""真实 PostgreSQL 隔离验证；仅针对独立 homeai_test 数据库。"""
import os
import uuid
import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from homeai.android_workspace import AndroidWorkspace, AndroidInput, AndroidInputFocus
from homeai.client_actions import principal_scope
from homeai.config import Settings
from homeai.db import Principal, database, scope

pytestmark = pytest.mark.skipif(os.environ.get('HOMEAI_INTEGRATION') != '1', reason='需要独立 PostgreSQL 测试数据库迁移到 0028')


def test_android_rls_scopes_input_and_admin_cannot_write_controls():
    settings = Settings()
    engine, factory = database(settings.database_url.rsplit('/', 1)[0] + '/homeai_test')
    household, other_household = str(uuid.uuid4()), str(uuid.uuid4())
    admin, alice, bob, outsider = [str(uuid.uuid4()) for _ in range(4)]
    identifier = str(uuid.uuid4())
    try:
        with factory() as db:
            for person, family, role in ((admin, household, 'infrastructure_owner'), (alice, household, 'adult'),
                                        (bob, household, 'adult'), (outsider, other_household, 'infrastructure_owner')):
                db.add(Principal(id=person, household_id=family, role=role, name='Android RLS 公开测试'))
            db.commit()
            scope(db, alice, household)
            row = AndroidWorkspace(id=identifier, owner_id=alice, household_id=household,
                serial='test-' + identifier, label='公开测试工作区', backend='emulator')
            db.add(row)
            db.flush()
            entry = AndroidInput(owner_id=alice, household_id=household, workspace_id=identifier,
                epoch=1, sequence=1, request_hash='public-test', status='SUCCEEDED')
            db.add(entry)
            focus = AndroidInputFocus(owner_id=alice, household_id=household, workspace_id=identifier,
                device_id='public-device', epoch=1, source='human', fingerprint='public-focus', expires_at=9999999999.0)
            db.add(focus)
            db.commit()
            input_id = entry.id
            focus_id = focus.id
        for principal, family in ((bob, household), (outsider, other_household)):
            with factory() as db:
                scope(db, principal, family)
                assert db.get(AndroidWorkspace, identifier) is None
                assert db.get(AndroidInput, input_id) is None
                assert db.get(AndroidInputFocus, focus_id) is None
        with factory() as db:
            scope(db, admin, household)
            # 管理员只获绑定元数据，不能直接改他人操作权或读取输入账本。
            row = db.get(AndroidWorkspace, identifier)
            assert row is not None
            assert db.get(AndroidInput, input_id) is None
            assert db.get(AndroidInputFocus, focus_id) is None
            result = db.query(AndroidWorkspace).filter_by(id=identifier).update({'mode': 'ai'})
            assert result == 0
            db.rollback()
            scope(db, admin, household)
            # 服务端验证家庭归属后显式切换规范身份才能建立/撤销绑定。
            with principal_scope(db, alice, household):
                row = db.get(AndroidWorkspace, identifier, populate_existing=True)
                row.revoked = True
            db.commit()
        with factory() as db:
            scope(db, alice, household)
            assert db.get(AndroidWorkspace, identifier).revoked is True
            forged = AndroidInput(owner_id=bob, household_id=household, workspace_id=identifier,
                epoch=1, sequence=2, request_hash='forged', status='PENDING')
            db.add(forged)
            with pytest.raises(DBAPIError):
                db.flush()
            db.rollback()
    finally:
        with factory() as db:
            scope(db, alice, household)
            db.query(AndroidInputFocus).filter_by(workspace_id=identifier).delete()
            db.query(AndroidInput).filter_by(workspace_id=identifier).delete()
            db.query(AndroidWorkspace).filter_by(id=identifier).delete()
            db.commit()
            db.query(Principal).filter(Principal.id.in_([admin, alice, bob, outsider])).delete(synchronize_session=False)
            db.commit()
        engine.dispose()
