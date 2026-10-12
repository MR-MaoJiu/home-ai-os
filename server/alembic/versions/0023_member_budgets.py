"""成员独立额度；默认继承，管理员只能配置本家庭。"""
from alembic import op
import sqlalchemy as sa
revision = '0023'
down_revision = '0022'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('member_budgets',
        sa.Column('member_id', sa.String(), sa.ForeignKey('principals.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('household_id', sa.String(), nullable=False), sa.Column('daily_tokens', sa.Integer(), nullable=True),
        sa.Column('daily_images', sa.Integer(), nullable=True), sa.Column('updated_at', sa.Float(), nullable=False),
        sa.CheckConstraint('daily_tokens IS NULL OR daily_tokens BETWEEN 0 AND 10000000', name='member_token_limit'),
        sa.CheckConstraint('daily_images IS NULL OR daily_images BETWEEN 0 AND 1000', name='member_image_limit'))
    op.create_index('ix_member_budgets_household_id', 'member_budgets', ['household_id'])
    op.execute('ALTER TABLE member_budgets ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE member_budgets FORCE ROW LEVEL SECURITY')
    family = "household_id=current_setting('homeai.household_id',true)"
    admin = "EXISTS (SELECT 1 FROM principals p WHERE p.id=current_setting('homeai.user_id',true) AND p.household_id=member_budgets.household_id AND p.role='infrastructure_owner')"
    target = 'EXISTS (SELECT 1 FROM principals target WHERE target.id=member_budgets.member_id AND target.household_id=member_budgets.household_id)'
    write = f'{family} AND {admin} AND {target}'
    op.execute(f"CREATE POLICY quota_read ON member_budgets FOR SELECT USING ({family} AND (member_id=current_setting('homeai.user_id',true) OR {admin}))")
    op.execute(f'CREATE POLICY quota_insert ON member_budgets FOR INSERT WITH CHECK ({write})')
    op.execute(f'CREATE POLICY quota_update ON member_budgets FOR UPDATE USING ({write}) WITH CHECK ({write})')
    op.execute(f'CREATE POLICY quota_delete ON member_budgets FOR DELETE USING ({write})')


def downgrade():
    op.drop_table('member_budgets')
