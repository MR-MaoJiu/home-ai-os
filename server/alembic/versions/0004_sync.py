"""设备同步快照、确认与批次幂等。"""
from alembic import op
import sqlalchemy as sa

revision='0004'
down_revision='0003'
branch_labels=None
depends_on=None


def upgrade():
    common=lambda:[sa.Column('id',sa.String(),primary_key=True),sa.Column('owner_id',sa.String(),nullable=False),sa.Column('household_id',sa.String(),nullable=False),sa.Column('device_id',sa.String(),nullable=False)]
    op.create_table('sync_snapshots',*common(),sa.Column('watermark',sa.Integer(),nullable=False),sa.Column('payload',sa.Text(),nullable=False),sa.Column('next_offset',sa.Integer(),nullable=False),sa.Column('expires_at',sa.Float(),nullable=False))
    op.create_table('sync_cursors',*common(),sa.Column('initialized',sa.Boolean(),nullable=False),sa.Column('acknowledged',sa.Integer(),nullable=False),sa.Column('offered',sa.Integer(),nullable=False),sa.UniqueConstraint('owner_id','device_id'))
    op.create_table('sync_receipts',*common(),sa.Column('batch_id',sa.String(),nullable=False),sa.Column('request_hash',sa.String(),nullable=False),sa.Column('payload',sa.Text(),nullable=False),sa.UniqueConstraint('owner_id','device_id','batch_id'))
    for table in ('sync_snapshots','sync_cursors','sync_receipts'):
        op.create_index('ix_'+table+'_owner',table,['owner_id'])
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY owner_access ON {table} USING (owner_id=current_setting('homeai.user_id',true) AND household_id=current_setting('homeai.household_id',true)) WITH CHECK (owner_id=current_setting('homeai.user_id',true) AND household_id=current_setting('homeai.household_id',true))")
    op.execute("DROP POLICY granted_records ON data_records")
    op.execute("CREATE POLICY granted_records ON data_records FOR SELECT USING (household_id=current_setting('homeai.household_id',true) AND NOT deleted AND sensitivity <> 'SECRET' AND id IN (SELECT record_id FROM grants WHERE grantee_id=current_setting('homeai.user_id',true)))")
    op.execute("""CREATE POLICY shared_record_events ON event_outbox FOR INSERT WITH CHECK (
      household_id=current_setting('homeai.household_id',true)
      AND kind IN ('record.changed','record.deleted','record.revoked')
      AND EXISTS (SELECT 1 FROM data_records r WHERE r.id=resource_id AND r.owner_id=current_setting('homeai.user_id',true))
      AND EXISTS (SELECT 1 FROM principals p WHERE p.id=event_outbox.owner_id AND p.household_id=event_outbox.household_id)
    )""")


def downgrade():
    op.execute('DROP POLICY shared_record_events ON event_outbox')
    for table in ('sync_receipts','sync_cursors','sync_snapshots'):op.drop_table(table)
    # 保留更严格的 SECRET 共享读取约束。
