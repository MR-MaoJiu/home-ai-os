"""持久化成员会话与消息，受应用层和 PostgreSQL 双重隔离。"""
from alembic import op
import sqlalchemy as sa
revision='0010'
down_revision='0009'
branch_labels=None
depends_on=None

def upgrade():
    op.create_table('conversations',sa.Column('id',sa.String(),primary_key=True),sa.Column('owner_id',sa.String(),nullable=False),sa.Column('household_id',sa.String(),nullable=False),sa.Column('title',sa.Text(),nullable=False),sa.Column('created_at',sa.Float(),nullable=False),sa.Column('updated_at',sa.Float(),nullable=False),sa.Column('next_sequence',sa.Integer(),nullable=False))
    op.create_table('conversation_turns',sa.Column('id',sa.String(),primary_key=True),sa.Column('owner_id',sa.String(),nullable=False),sa.Column('household_id',sa.String(),nullable=False),sa.Column('conversation_id',sa.String(),nullable=False),sa.Column('sequence',sa.Integer(),nullable=False),sa.Column('client_key',sa.String(),nullable=False),sa.Column('request_hash',sa.String(),nullable=False),sa.Column('user_message',sa.Text(),nullable=False),sa.Column('task_id',sa.String(),nullable=False),sa.Column('created_at',sa.Float(),nullable=False),sa.UniqueConstraint('conversation_id','sequence'),sa.UniqueConstraint('conversation_id','client_key'),sa.UniqueConstraint('task_id'))
    for table in ('conversations','conversation_turns'):
        op.create_index('ix_'+table+'_owner_id',table,['owner_id'])
        op.create_index('ix_'+table+'_household_id',table,['household_id'])
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        op.execute(f"CREATE POLICY owner_access ON {table} USING (owner_id=current_setting('homeai.user_id',true) AND household_id=current_setting('homeai.household_id',true)) WITH CHECK (owner_id=current_setting('homeai.user_id',true) AND household_id=current_setting('homeai.household_id',true))")
    op.create_index('ix_conversation_turns_conversation_id','conversation_turns',['conversation_id'])
    op.create_index('ix_conversation_turns_task_id','conversation_turns',['task_id'])

def downgrade():
    op.drop_table('conversation_turns');op.drop_table('conversations')
