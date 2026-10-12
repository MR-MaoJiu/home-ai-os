"""成员专属 Android 工作区和不会自动重放的输入日志。"""
from alembic import op
import sqlalchemy as sa

revision = '0027'
down_revision = '0026'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('android_workspaces',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('household_id', sa.String(), nullable=False),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('revoked', sa.Boolean(), nullable=False),
        sa.Column('serial', sa.String(), nullable=False),
        sa.Column('label', sa.String(), nullable=False),
        sa.Column('backend', sa.String(), nullable=False),
        sa.Column('packages', sa.Text(), nullable=False),
        sa.Column('mode', sa.String(), nullable=False),
        sa.Column('epoch', sa.Integer(), nullable=False),
        sa.Column('controller_device_id', sa.String(), nullable=True),
        sa.Column('lease_until', sa.Float(), nullable=False),
        sa.Column('frame_id', sa.String(), nullable=True),
        sa.Column('frame_digest', sa.String(), nullable=True),
        sa.Column('frame_width', sa.Integer(), nullable=False),
        sa.Column('frame_height', sa.Integer(), nullable=False),
        sa.Column('frame_at', sa.Float(), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.UniqueConstraint('owner_id'), sa.UniqueConstraint('serial'))
    op.create_table('android_inputs',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('household_id', sa.String(), nullable=False),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('workspace_id', sa.String(), sa.ForeignKey('android_workspaces.id', ondelete='CASCADE'), nullable=False),
        sa.Column('epoch', sa.Integer(), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('request_hash', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.UniqueConstraint('workspace_id', 'epoch', 'sequence'))
    for table in ('android_workspaces', 'android_inputs'):
        op.create_index('ix_' + table + '_household_id', table, ['household_id'])
        op.create_index('ix_' + table + '_owner_id', table, ['owner_id'])
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        family = "household_id=current_setting('homeai.household_id',true)"
        principal = f"EXISTS (SELECT 1 FROM principals p WHERE p.id=current_setting('homeai.user_id',true) AND p.household_id={table}.household_id)"
        owned = f"{family} AND {principal} AND owner_id=current_setting('homeai.user_id',true)"
        admin = f"EXISTS (SELECT 1 FROM principals p WHERE p.id=current_setting('homeai.user_id',true) AND p.household_id={table}.household_id AND p.role='infrastructure_owner')"
        read = f'{family} AND ({owned} OR {admin})' if table == 'android_workspaces' else owned
        canonical = '' if table == 'android_workspaces' else " AND EXISTS (SELECT 1 FROM android_workspaces w WHERE w.id=android_inputs.workspace_id AND w.owner_id=android_inputs.owner_id AND w.household_id=android_inputs.household_id)"
        op.execute(f'CREATE POLICY scope_read ON {table} FOR SELECT USING ({read})')
        op.execute(f'CREATE POLICY scope_insert ON {table} FOR INSERT WITH CHECK ({owned}{canonical})')
        op.execute(f'CREATE POLICY scope_update ON {table} FOR UPDATE USING ({owned}) WITH CHECK ({owned}{canonical})')
        op.execute(f'CREATE POLICY scope_delete ON {table} FOR DELETE USING ({owned})')
    op.create_index('ix_android_inputs_workspace_id', 'android_inputs', ['workspace_id'])


def downgrade():
    raise RuntimeError('Android 操作去重及设备归属墓碑不能通过降级删除')
