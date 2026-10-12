"""文字输入绑定窗口和控件身份，避免光标闪烁导致截图误判。"""
from alembic import op
import sqlalchemy as sa

revision = '0028'
down_revision = '0027'
branch_labels = None
depends_on = None


def upgrade():
    table = 'android_input_focus'
    op.create_table(table,
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('household_id', sa.String(), nullable=False),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('workspace_id', sa.String(), sa.ForeignKey('android_workspaces.id', ondelete='CASCADE'), nullable=False),
        sa.Column('device_id', sa.String(), nullable=False),
        sa.Column('epoch', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('fingerprint', sa.String(), nullable=False),
        sa.Column('expires_at', sa.Float(), nullable=False),
        sa.Column('consumed', sa.Boolean(), nullable=False))
    for key in ('household_id', 'owner_id', 'workspace_id'):
        op.create_index('ix_' + table + '_' + key, table, [key])
    op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
    owned = f"household_id=current_setting('homeai.household_id',true) AND owner_id=current_setting('homeai.user_id',true) AND EXISTS (SELECT 1 FROM principals p WHERE p.id=android_input_focus.owner_id AND p.household_id=android_input_focus.household_id)"
    canonical = "EXISTS (SELECT 1 FROM android_workspaces w WHERE w.id=android_input_focus.workspace_id AND w.owner_id=android_input_focus.owner_id AND w.household_id=android_input_focus.household_id)"
    op.execute(f'CREATE POLICY scope_read ON {table} FOR SELECT USING ({owned})')
    op.execute(f'CREATE POLICY scope_insert ON {table} FOR INSERT WITH CHECK ({owned} AND {canonical})')
    op.execute(f'CREATE POLICY scope_update ON {table} FOR UPDATE USING ({owned}) WITH CHECK ({owned} AND {canonical})')
    op.execute(f'CREATE POLICY scope_delete ON {table} FOR DELETE USING ({owned})')


def downgrade():
    raise RuntimeError('不能通过降级移除 Android 输入焦点身份绑定')
