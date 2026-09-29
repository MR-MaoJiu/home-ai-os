"""服务端持久通知箱、设备推送登记与可恢复投递账本。"""
from alembic import op
import sqlalchemy as sa

revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def owned():
    return [sa.Column('id', sa.String(), primary_key=True), sa.Column('household_id', sa.String(), nullable=False),
            sa.Column('owner_id', sa.String(), nullable=False)]


def upgrade():
    op.create_table('notifications', *owned(), sa.Column('event_key', sa.String(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False), sa.Column('visibility', sa.String(), nullable=False),
        sa.Column('task_id', sa.String()), sa.Column('conversation_id', sa.String()), sa.Column('automation_id', sa.String()),
        sa.Column('record_id', sa.String()), sa.Column('status', sa.String(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False), sa.Column('read_at', sa.Float()),
        sa.UniqueConstraint('owner_id', 'event_key'))
    op.create_index('ix_notifications_created_at', 'notifications', ['created_at'])
    op.create_table('push_registrations', *owned(), sa.Column('device_id', sa.String(), unique=True, nullable=False),
        sa.Column('token', sa.Text(), nullable=False), sa.Column('token_digest', sa.String(), nullable=False),
        sa.Column('environment', sa.String(), nullable=False), sa.Column('authorization', sa.String(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False), sa.Column('updated_at', sa.Float(), nullable=False))
    op.create_table('push_deliveries', *owned(), sa.Column('notification_id', sa.String(), nullable=False),
        sa.Column('device_id', sa.String(), nullable=False), sa.Column('status', sa.String(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False), sa.Column('retry_at', sa.Float(), nullable=False),
        sa.Column('last_error', sa.String()), sa.Column('accepted_at', sa.Float()),
        sa.UniqueConstraint('notification_id', 'device_id'))
    op.create_index('ix_push_deliveries_notification_id', 'push_deliveries', ['notification_id'])
    for table in ('notifications', 'push_registrations', 'push_deliveries'):
        for column in ('owner_id', 'household_id'):
            op.create_index('ix_' + table + '_' + column, table, [column])
        op.execute('ALTER TABLE ' + table + ' ENABLE ROW LEVEL SECURITY')
        op.execute('ALTER TABLE ' + table + ' FORCE ROW LEVEL SECURITY')
        condition = "owner_id=current_setting('homeai.user_id',true) AND household_id=current_setting('homeai.household_id',true)"
        op.execute('CREATE POLICY owner_isolation ON ' + table + ' USING (' + condition + ') WITH CHECK (' + condition + ')')
    # 升级时不向用户补推全部历史任务。迁移之后的事件继续可靠消费。
    op.execute("INSERT INTO event_consumptions (id,created_at) SELECT 'notification:' || event_id,extract(epoch from now()) FROM event_outbox WHERE kind IN ('task.updated','task.approval_required') ON CONFLICT DO NOTHING")


def downgrade():
    for table in ('push_deliveries', 'push_registrations', 'notifications'):
        op.drop_table(table)
