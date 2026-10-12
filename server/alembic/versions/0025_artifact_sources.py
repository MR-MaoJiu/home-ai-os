"""Shell 派生成果与规范来源版本绑定，删除或撤权后所有读取入口同时失效。"""
from alembic import op
import sqlalchemy as sa

revision = '0025'
down_revision = '0024'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('artifact_sources',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('household_id', sa.String(), nullable=False), sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('record_id', sa.String(), sa.ForeignKey('data_records.id', ondelete='CASCADE'), nullable=False),
        sa.Column('source_id', sa.String(), nullable=False), sa.Column('source_version', sa.Integer(), nullable=False),
        sa.UniqueConstraint('record_id', 'source_id'),
        sa.CheckConstraint('source_version > 0', name='artifact_source_positive_version'),
        sa.CheckConstraint('record_id != source_id', name='artifact_source_not_self'))
    for column in ('household_id', 'owner_id', 'record_id', 'source_id'):
        op.create_index('ix_artifact_sources_' + column, 'artifact_sources', [column])
    op.execute('ALTER TABLE artifact_sources ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE artifact_sources FORCE ROW LEVEL SECURITY')
    household = "household_id=current_setting('homeai.household_id',true)"
    owned = household + " AND owner_id=current_setting('homeai.user_id',true)"
    # 家庭内读取来源索引用于检查共享成果；正文仍受 data_records 的 RLS 和应用层授权保护。
    op.execute(f'CREATE POLICY scope_read ON artifact_sources FOR SELECT USING ({household})')
    op.execute(f'CREATE POLICY scope_insert ON artifact_sources FOR INSERT WITH CHECK ({owned})')
    op.execute(f'CREATE POLICY scope_update ON artifact_sources FOR UPDATE USING ({owned}) WITH CHECK ({owned})')
    op.execute(f'CREATE POLICY scope_delete ON artifact_sources FOR DELETE USING ({owned})')


def downgrade():
    if op.get_bind().execute(sa.text('SELECT EXISTS (SELECT 1 FROM artifact_sources)')).scalar():
        raise RuntimeError('存在成果来源约束，不能通过回退删除访问控制关系')
    op.drop_table('artifact_sources')
