"""加密备用通道的持久派发凭据；响应丢失不能导致重复副作用。"""
from alembic import op
import sqlalchemy as sa
revision='0032'
down_revision='0031'
branch_labels=None
depends_on=None

def upgrade():
    op.create_table('encrypted_relay_receipts',sa.Column('id',sa.String(),primary_key=True),
        sa.Column('owner_id',sa.String(),nullable=False),sa.Column('household_id',sa.String(),nullable=False),
        sa.Column('device_id',sa.String(),nullable=False),sa.Column('request_hash',sa.String(),nullable=False),
        sa.Column('response_key',sa.Text(),nullable=False),sa.Column('response',sa.Text(),nullable=True),sa.Column('expires',sa.Float(),nullable=False))
    for key in ('owner_id','household_id'):op.create_index('ix_encrypted_relay_receipts_'+key,'encrypted_relay_receipts',[key])
    op.execute('ALTER TABLE encrypted_relay_receipts ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE encrypted_relay_receipts FORCE ROW LEVEL SECURITY')
    predicate="owner_id=current_setting('homeai.user_id',true) AND household_id=current_setting('homeai.household_id',true)"
    op.execute(f'CREATE POLICY owner_scope ON encrypted_relay_receipts USING ({predicate}) WITH CHECK ({predicate})')

def downgrade():raise RuntimeError('派发去重凭据不能通过降级丢弃')
