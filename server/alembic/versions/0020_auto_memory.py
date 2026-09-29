"""聊天偏好的可追溯学习与遗忘墓碑，不保存模型猜测。"""
from alembic import op
import sqlalchemy as sa
revision='0020'
down_revision='0019'
branch_labels=None
depends_on=None


def owned():
    return [sa.Column('id',sa.String(),primary_key=True),sa.Column('owner_id',sa.String(),nullable=False),sa.Column('household_id',sa.String(),nullable=False)]


def upgrade():
    op.create_table('memory_learning',*owned(),sa.Column('turn_id',sa.String(),nullable=False),sa.Column('conversation_id',sa.String(),nullable=False),
        sa.Column('fingerprint',sa.String(),nullable=False),sa.Column('preference_key',sa.String(),nullable=False),sa.Column('polarity',sa.String(),nullable=False),
        sa.Column('classification',sa.String(),nullable=False),sa.Column('status',sa.String(),nullable=False),sa.Column('record_id',sa.String()),
        sa.Column('candidate_id',sa.String()),sa.Column('conflict_record_id',sa.String()),sa.Column('created_at',sa.Float(),nullable=False),sa.UniqueConstraint('owner_id','turn_id'))
    op.create_table('memory_forget_tombstones',*owned(),sa.Column('fingerprint',sa.String(),nullable=False),sa.Column('created_at',sa.Float(),nullable=False),sa.UniqueConstraint('owner_id','fingerprint'))
    for table in ('memory_learning','memory_forget_tombstones'):
        for column in ('owner_id','household_id','fingerprint'):op.create_index('ix_'+table+'_'+column,table,[column])
        op.execute('ALTER TABLE '+table+' ENABLE ROW LEVEL SECURITY');op.execute('ALTER TABLE '+table+' FORCE ROW LEVEL SECURITY')
        condition="owner_id=current_setting('homeai.user_id',true) AND household_id=current_setting('homeai.household_id',true)"
        op.execute('CREATE POLICY owner_isolation ON '+table+' USING ('+condition+') WITH CHECK ('+condition+')')
    for column in ('preference_key','candidate_id'):op.create_index('ix_memory_learning_'+column,'memory_learning',[column])


def downgrade():
    op.drop_table('memory_forget_tombstones');op.drop_table('memory_learning')
