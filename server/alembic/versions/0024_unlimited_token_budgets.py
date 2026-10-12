"""每日 Token 额度支持明确不限；与继承、零额度分开保存。"""
from alembic import op
import sqlalchemy as sa
revision = '0024'
down_revision = '0023'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column('member_budgets','daily_tokens',existing_type=sa.Integer(),type_=sa.BigInteger())
    op.drop_constraint('member_token_limit','member_budgets',type_='check')
    op.create_check_constraint('member_token_limit','member_budgets','daily_tokens IS NULL OR daily_tokens BETWEEN 0 AND 9007199254740991')
    op.add_column('member_budgets',sa.Column('tokens_unlimited',sa.Boolean(),nullable=False,server_default=sa.false()))
    op.create_check_constraint('member_unlimited_token_shape','member_budgets','NOT tokens_unlimited OR daily_tokens IS NULL')
    for column in ('reserved','charged'):
        op.alter_column('model_usage',column,existing_type=sa.Integer(),type_=sa.BigInteger(),existing_nullable=False)


def downgrade():
    unsafe=op.get_bind().execute(sa.text('SELECT EXISTS (SELECT 1 FROM member_budgets WHERE tokens_unlimited OR daily_tokens>10000000) OR EXISTS (SELECT 1 FROM model_usage WHERE reserved>2147483647 OR charged>2147483647)')).scalar()
    if unsafe:
        raise RuntimeError('存在不限额度或超出旧类型范围的数据，不能自动缩减或删除后回退')
    op.drop_constraint('member_unlimited_token_shape','member_budgets',type_='check')
    op.drop_column('member_budgets','tokens_unlimited')
    op.drop_constraint('member_token_limit','member_budgets',type_='check')
    op.create_check_constraint('member_token_limit','member_budgets','daily_tokens IS NULL OR daily_tokens BETWEEN 0 AND 10000000')
    op.alter_column('member_budgets','daily_tokens',existing_type=sa.BigInteger(),type_=sa.Integer())
    for column in ('reserved','charged'):
        op.alter_column('model_usage',column,existing_type=sa.BigInteger(),type_=sa.Integer(),existing_nullable=False)
