"""事件自动化投递、规范事件元数据与因果链。"""
from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade():
    for name in ('record_kind', 'record_source', 'record_owner_id'):
        op.add_column('event_outbox', sa.Column(name, sa.String(), nullable=True))
    op.add_column('event_outbox', sa.Column('record_version', sa.Integer(), nullable=True))
    op.add_column('event_outbox', sa.Column('automation_chain', sa.Text(), nullable=False, server_default='[]'))
    for name, type_, default in (
        ('trigger_kind', sa.String(), 'cron'), ('include_shared', sa.Boolean(), 'false'),
        ('cooldown_seconds', sa.Integer(), '0'), ('created_at', sa.Float(), '0'),
        ('last_trigger_at', sa.Float(), '0'),
    ):
        op.add_column('automations', sa.Column(name, type_, nullable=False, server_default=default))
    for name in ('event_type', 'record_kind', 'record_source'):
        op.add_column('automations', sa.Column(name, sa.String(), nullable=True))
    op.create_table('automation_deliveries',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('household_id', sa.String(), nullable=False),
        sa.Column('automation_id', sa.String(), nullable=False),
        sa.Column('event_id', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('task_id', sa.String(), nullable=True),
        sa.Column('reason', sa.String(), nullable=True),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.UniqueConstraint('automation_id', 'event_id'))
    op.create_index('ix_automation_delivery_pending', 'automation_deliveries', ['owner_id', 'status', 'created_at'])
    op.execute('ALTER TABLE automation_deliveries ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE automation_deliveries FORCE ROW LEVEL SECURITY')
    op.execute("CREATE POLICY owner_access ON automation_deliveries USING (owner_id=current_setting('homeai.user_id',true) AND household_id=current_setting('homeai.household_id',true)) WITH CHECK (owner_id=current_setting('homeai.user_id',true) AND household_id=current_setting('homeai.household_id',true))")


def downgrade():
    op.drop_table('automation_deliveries')
    for name in ('event_type', 'record_kind', 'record_source', 'trigger_kind', 'include_shared', 'cooldown_seconds', 'created_at', 'last_trigger_at'):
        op.drop_column('automations', name)
    for name in ('record_kind', 'record_source', 'record_owner_id', 'record_version', 'automation_chain'):
        op.drop_column('event_outbox', name)
