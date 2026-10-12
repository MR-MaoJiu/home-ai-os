"""安卓设备占用关联服务端任务，便于客户端展示与人工接管隔离。"""
from alembic import op
import sqlalchemy as sa

revision = '0030'
down_revision = '0029'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('android_workspaces', sa.Column('active_task_id', sa.String(), nullable=True))


def downgrade():
    raise RuntimeError('不能通过降级丢弃安卓任务操作权关联')
