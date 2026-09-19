"""内置服务部署状态；仅基础设施管理员通过受控接口修改。"""
from alembic import op
import sqlalchemy as sa
revision='0012'
down_revision='0011'
branch_labels=None
depends_on=None

def upgrade():
    op.create_table('builtin_deployments',sa.Column('id',sa.String(),primary_key=True),sa.Column('variant',sa.String(),nullable=False),sa.Column('enabled',sa.Boolean(),nullable=False),sa.Column('status',sa.String(),nullable=False),sa.Column('error',sa.Text()),sa.Column('updated_at',sa.Float(),nullable=False))

def downgrade():op.drop_table('builtin_deployments')
