"""设备自动审核默认关闭；现有配对和业务数据保持不变。"""
from alembic import op
import sqlalchemy as sa

revision = '0033'
down_revision = '0032'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('devices', sa.Column('auto_review_enabled', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column('devices', 'auto_review_enabled')
