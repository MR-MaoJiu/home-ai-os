"""客户端数据请求、24小时任务级授权与双主体读取隔离。"""
from alembic import op
import sqlalchemy as sa

revision='0018'
down_revision='0017'
branch_labels=None
depends_on=None


def owned():
    return [sa.Column('id',sa.String(),primary_key=True),sa.Column('household_id',sa.String(),nullable=False),sa.Column('owner_id',sa.String(),nullable=False)]


def upgrade():
    op.create_table('client_actions',*owned(),sa.Column('requester_id',sa.String(),nullable=False),sa.Column('task_id',sa.String(),nullable=False),
        sa.Column('invocation_id',sa.String()),sa.Column('request_key',sa.String(),nullable=False),sa.Column('request_hash',sa.String(),nullable=False),
        sa.Column('kind',sa.String(),nullable=False),sa.Column('payload',sa.Text(),nullable=False),sa.Column('status',sa.String(),nullable=False),
        sa.Column('created_at',sa.Float(),nullable=False),sa.Column('expires_at',sa.Float(),nullable=False),sa.Column('responded_at',sa.Float()),
        sa.Column('response_key',sa.String()),sa.Column('response_hash',sa.String()),sa.Column('response',sa.Text()),sa.Column('notification_id',sa.String()),
        sa.UniqueConstraint('requester_id','task_id','request_key'))
    op.create_table('task_data_grants',*owned(),sa.Column('requester_id',sa.String(),nullable=False),sa.Column('task_id',sa.String(),nullable=False),
        sa.Column('action_id',sa.String(),nullable=False),sa.Column('record_id',sa.String(),nullable=False),sa.Column('record_version',sa.Integer(),nullable=False),
        sa.Column('expires_at',sa.Float(),nullable=False),sa.Column('revoked',sa.Boolean(),nullable=False),sa.UniqueConstraint('action_id','record_id'))
    for table in ('client_actions','task_data_grants'):
        for column in ('household_id','owner_id','requester_id','task_id'):
            op.create_index('ix_'+table+'_'+column,table,[column])
        op.execute('ALTER TABLE '+table+' ENABLE ROW LEVEL SECURITY')
        op.execute('ALTER TABLE '+table+' FORCE ROW LEVEL SECURITY')
        owner="owner_id=current_setting('homeai.user_id',true) AND household_id=current_setting('homeai.household_id',true)"
        reader="(owner_id=current_setting('homeai.user_id',true) OR requester_id=current_setting('homeai.user_id',true)) AND household_id=current_setting('homeai.household_id',true)"
        op.execute('CREATE POLICY task_participants_read ON '+table+' FOR SELECT USING ('+reader+')')
        for action in ('INSERT','UPDATE','DELETE'):
            clause=(' USING ('+owner+')' if action!='INSERT' else '')+(' WITH CHECK ('+owner+')' if action!='DELETE' else '')
            op.execute('CREATE POLICY owner_'+action.lower()+' ON '+table+' FOR '+action+clause)
    for column in ('action_id','record_id'):
        op.create_index('ix_task_data_grants_'+column,'task_data_grants',[column])


def downgrade():
    op.drop_table('task_data_grants');op.drop_table('client_actions')
