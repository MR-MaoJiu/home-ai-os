"""为真人连续手势保存短期已发布帧标识，不保存画面。"""
from alembic import op
import sqlalchemy as sa

revision = '0029'
down_revision = '0028'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('android_workspaces', sa.Column('recent_frames', sa.Text(), nullable=False, server_default='[]'))


def downgrade():
    raise RuntimeError('不能通过降级移除真人手势的帧身份校验')
