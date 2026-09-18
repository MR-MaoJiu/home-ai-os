"""健康与位置的持续共享授权独立于资料和记忆。"""
from alembic import op
import sqlalchemy as sa
revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('sharing_rules', sa.Column('id', sa.String(), primary_key=True), sa.Column('household_id', sa.String(), nullable=False), sa.Column('owner_id', sa.String(), nullable=False), sa.Column('category', sa.String(), nullable=False), sa.Column('grantee_id', sa.String(), nullable=False), sa.UniqueConstraint('owner_id', 'category', 'grantee_id'))
    for column in ('owner_id', 'household_id'):
        op.create_index('ix_sharing_rules_' + column, 'sharing_rules', [column])
    op.execute('ALTER TABLE sharing_rules ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE sharing_rules FORCE ROW LEVEL SECURITY')
    op.execute("CREATE POLICY owner_access ON sharing_rules USING (owner_id=current_setting('homeai.user_id',true) AND household_id=current_setting('homeai.household_id',true)) WITH CHECK (owner_id=current_setting('homeai.user_id',true) AND household_id=current_setting('homeai.household_id',true))")


def downgrade():
    op.drop_table('sharing_rules')
