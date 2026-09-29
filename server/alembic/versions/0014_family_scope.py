"""个人与家庭范围；保留原始资料，不把旧定向授权扩大为全家。"""
from alembic import op
import sqlalchemy as sa
revision='0014'
down_revision='0013'
branch_labels=None
depends_on=None

def upgrade():
    op.add_column('data_records',sa.Column('visibility',sa.String(),nullable=False,server_default='personal'))
    op.add_column('automations',sa.Column('visibility',sa.String(),nullable=False,server_default='personal'))
    op.add_column('automations',sa.Column('instruction',sa.Text(),nullable=True))
    op.add_column('memory_candidates',sa.Column('conversation_id',sa.String(),nullable=True))
    op.add_column('memory_candidates',sa.Column('turn_id',sa.String(),nullable=True))
    # 发送删除本地共享缓存的墓碑；不删除来源资料。
    op.execute("INSERT INTO event_outbox(event_id,household_id,owner_id,kind,resource_id,record_kind,record_source,record_owner_id,record_version,automation_chain,published,created_at) SELECT gen_random_uuid()::text,g.household_id,g.grantee_id,'record.revoked',g.record_id,r.kind,r.source,r.owner_id,r.version,'[]',false,extract(epoch from now()) FROM grants g JOIN data_records r ON r.id=g.record_id")
    op.execute('DELETE FROM grants')
    op.execute('DELETE FROM sharing_rules')
    op.execute('DELETE FROM sync_snapshots')
    op.execute('DROP POLICY IF EXISTS granted_records ON data_records')
    op.execute("CREATE POLICY family_records ON data_records FOR SELECT USING (household_id=current_setting('homeai.household_id',true) AND visibility='family' AND NOT deleted AND sensitivity<>'SECRET' AND kind NOT LIKE 'memory.%')")
    op.execute("CREATE POLICY family_automations ON automations FOR SELECT USING (household_id=current_setting('homeai.household_id',true) AND visibility='family')")

def downgrade():
    op.execute('DROP POLICY IF EXISTS family_records ON data_records')
    op.execute('DROP POLICY IF EXISTS family_automations ON automations')
    op.execute("CREATE POLICY granted_records ON data_records FOR SELECT USING (household_id=current_setting('homeai.household_id',true) AND NOT deleted AND sensitivity<>'SECRET' AND id IN (SELECT record_id FROM grants WHERE grantee_id=current_setting('homeai.user_id',true)))")
    op.drop_column('memory_candidates','turn_id');op.drop_column('memory_candidates','conversation_id')
    op.drop_column('automations','instruction');op.drop_column('automations','visibility');op.drop_column('data_records','visibility')
