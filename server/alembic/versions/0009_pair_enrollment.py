"""短期配对引导与幂等结果，与身份凭据采用相同应用层访问边界。"""
from alembic import op
import sqlalchemy as sa
revision='0009'
down_revision='0008'
branch_labels=None
depends_on=None

def upgrade():
    op.create_table('pair_enrollments',sa.Column('id',sa.String(),primary_key=True),sa.Column('household_id',sa.String(),nullable=False),sa.Column('user_id',sa.String(),nullable=False),sa.Column('expires',sa.Float(),nullable=False),sa.Column('ciphertext',sa.Text(),nullable=False))

def downgrade():
    op.drop_table('pair_enrollments')
