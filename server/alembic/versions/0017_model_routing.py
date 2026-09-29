"""模型用途、验证、费用预留和加密脱敏映射。"""
from alembic import op
import sqlalchemy as sa
revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('model_configurations', sa.Column('household_id', sa.String(), primary_key=True), sa.Column('configuration', sa.Text(), nullable=False), sa.Column('updated_at', sa.Float(), nullable=False))
    op.create_table('model_verifications', sa.Column('id', sa.String(), primary_key=True), sa.Column('household_id', sa.String(), nullable=False), sa.Column('provider_id', sa.String(), nullable=False), sa.Column('fingerprint', sa.String(), nullable=False), sa.Column('report', sa.Text(), nullable=False), sa.Column('verified_at', sa.Float(), nullable=False))
    op.create_table('model_usage', sa.Column('id', sa.String(), primary_key=True), sa.Column('household_id', sa.String(), nullable=False), sa.Column('owner_id', sa.String(), nullable=False), sa.Column('task_id', sa.String(), nullable=False), sa.Column('call_key', sa.String(), unique=True, nullable=False), sa.Column('day', sa.String(), nullable=False), sa.Column('reserved', sa.Integer(), nullable=False), sa.Column('charged', sa.Integer(), nullable=False), sa.Column('status', sa.String(), nullable=False))
    op.create_table('redaction_artifacts', sa.Column('id', sa.String(), primary_key=True), sa.Column('household_id', sa.String(), nullable=False), sa.Column('owner_id', sa.String(), nullable=False), sa.Column('task_id', sa.String(), nullable=False), sa.Column('payload', sa.Text(), nullable=False), sa.Column('expires_at', sa.Float(), nullable=False))
    for table in ('model_configurations', 'model_verifications', 'model_usage', 'redaction_artifacts'):
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        household = "household_id=current_setting('homeai.household_id',true)"
        if table == 'redaction_artifacts':
            condition = household + " AND owner_id=current_setting('homeai.user_id',true)"
            op.execute(f'CREATE POLICY scope_access ON {table} USING ({condition}) WITH CHECK ({condition})')
        else:
            # 用量只有服务端可查询，成员预算须看到家庭所有成员预留总和。
            write = household + (" AND owner_id=current_setting('homeai.user_id',true)" if table == 'model_usage' else " AND EXISTS (SELECT 1 FROM principals WHERE id=current_setting('homeai.user_id',true) AND role='infrastructure_owner')")
            op.execute(f'CREATE POLICY scope_read ON {table} FOR SELECT USING ({household})')
            for operation in ('INSERT', 'UPDATE', 'DELETE'):
                rule = f'WITH CHECK ({write})' if operation == 'INSERT' else f'USING ({write})' + (f' WITH CHECK ({write})' if operation == 'UPDATE' else '')
                op.execute(f'CREATE POLICY scope_{operation.lower()} ON {table} FOR {operation} {rule}')
    op.create_index('ix_model_usage_household_day', 'model_usage', ['household_id', 'day'])
    op.create_index('ix_redaction_artifacts_task_id', 'redaction_artifacts', ['task_id'])


def downgrade():
    for table in ('redaction_artifacts', 'model_usage', 'model_verifications', 'model_configurations'):
        op.drop_table(table)
