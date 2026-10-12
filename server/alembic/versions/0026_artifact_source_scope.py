"""来源映射必须同时匹配成果、原件及成员的规范归属，防止伪造关系。"""
from alembic import op

revision = '0026'
down_revision = '0025'
branch_labels = None
depends_on = None


def upgrade():
    household = "household_id=current_setting('homeai.household_id',true) AND EXISTS (SELECT 1 FROM principals p WHERE p.id=current_setting('homeai.user_id',true) AND p.household_id=artifact_sources.household_id)"
    owned = household + " AND owner_id=current_setting('homeai.user_id',true)"
    canonical = "EXISTS (SELECT 1 FROM data_records r WHERE r.id=artifact_sources.record_id AND r.owner_id=artifact_sources.owner_id AND r.household_id=artifact_sources.household_id) AND EXISTS (SELECT 1 FROM data_records s WHERE s.id=artifact_sources.source_id AND s.owner_id=artifact_sources.owner_id AND s.household_id=artifact_sources.household_id)"
    op.execute('DROP POLICY scope_read ON artifact_sources')
    op.execute('DROP POLICY scope_insert ON artifact_sources')
    op.execute('DROP POLICY scope_update ON artifact_sources')
    op.execute('DROP POLICY scope_delete ON artifact_sources')
    op.execute(f'CREATE POLICY scope_read ON artifact_sources FOR SELECT USING ({household})')
    op.execute(f'CREATE POLICY scope_insert ON artifact_sources FOR INSERT WITH CHECK ({owned} AND {canonical})')
    op.execute(f'CREATE POLICY scope_update ON artifact_sources FOR UPDATE USING ({owned}) WITH CHECK ({owned} AND {canonical})')
    op.execute(f'CREATE POLICY scope_delete ON artifact_sources FOR DELETE USING ({owned})')


def downgrade():
    raise RuntimeError('不能通过降级移除成果来源的规范归属校验')
