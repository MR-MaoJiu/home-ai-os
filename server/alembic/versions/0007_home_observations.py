"""Core 持有家居最新观察值与连接状态，独立于 Provider 生命周期。"""
from alembic import op
import sqlalchemy as sa
revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('home_observations', sa.Column('id', sa.String(), primary_key=True),
        sa.Column('owner_id', sa.String(), nullable=False), sa.Column('household_id', sa.String(), nullable=False),
        sa.Column('provider_id', sa.String(), nullable=False), sa.Column('entity_id', sa.String(), nullable=False),
        sa.Column('payload', sa.Text(), nullable=False), sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('observed_at', sa.Float(), nullable=False), sa.UniqueConstraint('owner_id', 'provider_id', 'entity_id'))
    op.create_table('home_connections', sa.Column('id', sa.String(), primary_key=True),
        sa.Column('owner_id', sa.String(), nullable=False), sa.Column('household_id', sa.String(), nullable=False),
        sa.Column('provider_id', sa.String(), nullable=False), sa.Column('status', sa.String(), nullable=False),
        sa.Column('error_type', sa.String(), nullable=True), sa.Column('updated_at', sa.Float(), nullable=False),
        sa.UniqueConstraint('owner_id', 'provider_id'))
    for table in ('home_observations', 'home_connections'):
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        op.execute(f"CREATE POLICY owner_access ON {table} USING (owner_id=current_setting('homeai.user_id',true) AND household_id=current_setting('homeai.household_id',true)) WITH CHECK (owner_id=current_setting('homeai.user_id',true) AND household_id=current_setting('homeai.household_id',true))")


def downgrade():
    op.drop_table('home_connections')
    op.drop_table('home_observations')
