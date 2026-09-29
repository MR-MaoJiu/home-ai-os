"""为每个成员绑定唯一持续会话，保留旧对话和消息。"""
from alembic import op
import sqlalchemy as sa
revision='0016'
down_revision='0015'
branch_labels=None
depends_on=None

def upgrade():
    op.create_table('default_conversations',sa.Column('id',sa.String(),primary_key=True),sa.Column('owner_id',sa.String(),nullable=False),sa.Column('household_id',sa.String(),nullable=False),sa.Column('conversation_id',sa.String(),nullable=False),sa.UniqueConstraint('owner_id'))
    for column in ('owner_id','household_id'):op.create_index('ix_default_conversations_'+column,'default_conversations',[column])
    op.execute('ALTER TABLE default_conversations ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE default_conversations FORCE ROW LEVEL SECURITY')
    op.execute("CREATE POLICY owner_access ON default_conversations USING (owner_id=current_setting('homeai.user_id',true) AND household_id=current_setting('homeai.household_id',true)) WITH CHECK (owner_id=current_setting('homeai.user_id',true) AND household_id=current_setting('homeai.household_id',true))")

def downgrade():op.drop_table('default_conversations')
