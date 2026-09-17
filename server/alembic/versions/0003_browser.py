"""网页账户与登录限流。"""
from alembic import op
import sqlalchemy as sa
revision='0003'
down_revision='0002'
branch_labels=None
depends_on=None

def upgrade():
    op.create_table('browser_accounts',sa.Column('username',sa.String(),primary_key=True),sa.Column('user_id',sa.String(),unique=True,nullable=False),sa.Column('password_hash',sa.Text(),nullable=False),sa.Column('totp_secret',sa.Text(),nullable=False),sa.Column('totp_enabled',sa.Boolean(),nullable=False),sa.Column('last_totp_counter',sa.Integer(),nullable=False))
    op.create_table('login_attempts',sa.Column('id',sa.String(),primary_key=True),sa.Column('failures',sa.Integer(),nullable=False),sa.Column('window_start',sa.Float(),nullable=False))

def downgrade():
    op.drop_table('login_attempts')
    op.drop_table('browser_accounts')
