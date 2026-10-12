"""图片张数独立预留，未知结果仍计入额度。"""
from alembic import op
import sqlalchemy as sa
revision = '0022'
down_revision = '0021'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('image_usage', sa.Column('id', sa.String(), primary_key=True),
        sa.Column('household_id', sa.String(), nullable=False), sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('task_id', sa.String(), nullable=False), sa.Column('call_key', sa.String(), nullable=False, unique=True),
        sa.Column('day', sa.String(), nullable=False), sa.Column('charged', sa.Integer(), nullable=False), sa.Column('status', sa.String(), nullable=False))
    op.create_index('ix_image_usage_household_day', 'image_usage', ['household_id', 'day'])
    op.execute('ALTER TABLE image_usage ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE image_usage FORCE ROW LEVEL SECURITY')
    household = "household_id=current_setting('homeai.household_id',true)"
    owned = household + " AND owner_id=current_setting('homeai.user_id',true)"
    op.execute(f'CREATE POLICY scope_read ON image_usage FOR SELECT USING ({household})')
    op.execute(f'CREATE POLICY scope_insert ON image_usage FOR INSERT WITH CHECK ({owned})')
    op.execute(f'CREATE POLICY scope_update ON image_usage FOR UPDATE USING ({owned}) WITH CHECK ({owned})')
    op.execute(f'CREATE POLICY scope_delete ON image_usage FOR DELETE USING ({owned})')


def downgrade():
    op.drop_table('image_usage')
