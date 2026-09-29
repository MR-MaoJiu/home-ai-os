"""遗忘记忆时关联原聊天轮次，防止旧摘要重新引入事实。"""
from alembic import op
import sqlalchemy as sa
revision='0021'
down_revision='0020'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('memory_forget_sources',sa.Column('id',sa.String(),primary_key=True),sa.Column('owner_id',sa.String(),nullable=False),
        sa.Column('household_id',sa.String(),nullable=False),sa.Column('turn_id',sa.String(),nullable=False),
        sa.Column('created_at',sa.Float(),nullable=False),sa.UniqueConstraint('owner_id','turn_id'))
    for column in ('owner_id','household_id','turn_id'):op.create_index('ix_memory_forget_sources_'+column,'memory_forget_sources',[column])
    op.execute('ALTER TABLE memory_forget_sources ENABLE ROW LEVEL SECURITY');op.execute('ALTER TABLE memory_forget_sources FORCE ROW LEVEL SECURITY')
    condition="owner_id=current_setting('homeai.user_id',true) AND household_id=current_setting('homeai.household_id',true)"
    op.execute('CREATE POLICY owner_isolation ON memory_forget_sources USING ('+condition+') WITH CHECK ('+condition+')')


def downgrade():op.drop_table('memory_forget_sources')
