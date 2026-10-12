"""按成员隔离的通话、VoIP设备与持久语音轮次。"""
from alembic import op
import sqlalchemy as sa
revision='0031'
down_revision='0030'
branch_labels=None
depends_on=None

def base():
    return [sa.Column('id',sa.String(),primary_key=True),sa.Column('household_id',sa.String(),nullable=False),sa.Column('owner_id',sa.String(),nullable=False)]

def upgrade():
    op.create_table('voice_preferences',*base(),sa.Column('payload',sa.Text(),nullable=False),sa.UniqueConstraint('owner_id'))
    op.create_table('voip_registrations',*base(),sa.Column('device_id',sa.String(),nullable=False),sa.Column('token',sa.Text(),nullable=False),sa.Column('environment',sa.String(),nullable=False),sa.Column('updated_at',sa.Float(),nullable=False),sa.UniqueConstraint('device_id'))
    op.create_table('voice_calls',*base(),*[sa.Column(k,sa.String(),nullable=False) for k in ('request_key','target_device_id','direction','status','push_status')],sa.Column('source_task_id',sa.String(),nullable=True),sa.Column('payload',sa.Text(),nullable=False),*[sa.Column(k,sa.Float(),nullable=False) for k in ('created_at','expires_at','last_seen')],sa.UniqueConstraint('owner_id','request_key'))
    op.create_table('voice_turns',*base(),*[sa.Column(k,sa.String(),nullable=False) for k in ('call_id','client_key','request_hash','status')],sa.Column('payload',sa.Text(),nullable=False),sa.Column('created_at',sa.Float(),nullable=False),sa.UniqueConstraint('call_id','client_key'))
    op.create_index('ix_voice_turns_call_id','voice_turns',['call_id'])
    for name in ('voice_preferences','voip_registrations','voice_calls','voice_turns'):
        for key in ('owner_id','household_id'):op.create_index('ix_'+name+'_'+key,name,[key])
        op.execute(f'ALTER TABLE {name} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {name} FORCE ROW LEVEL SECURITY')
        rule=f"owner_id=current_setting('homeai.user_id',true) AND household_id=current_setting('homeai.household_id',true) AND EXISTS(SELECT 1 FROM principals p WHERE p.id=owner_id AND p.household_id={name}.household_id)"
        op.execute(f'CREATE POLICY scope_all ON {name} USING ({rule}) WITH CHECK ({rule})')

def downgrade():
    raise RuntimeError('通话去重记录不能通过降级删除')
