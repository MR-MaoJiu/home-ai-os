"""家居观察者租约，允许多个 Core 进程安全接管。"""
from alembic import op
import sqlalchemy as sa
revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('home_connections', sa.Column('lease_owner', sa.String(), nullable=True))
    op.add_column('home_connections', sa.Column('lease_until', sa.Float(), nullable=False, server_default='0'))


def downgrade():
    op.drop_column('home_connections', 'lease_until')
    op.drop_column('home_connections', 'lease_owner')
