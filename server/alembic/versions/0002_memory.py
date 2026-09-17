"""记忆候选、派生索引任务和向量扩展。"""
from alembic import op
import sqlalchemy as sa

revision='0002'
down_revision='0001'
branch_labels=None
depends_on=None


def upgrade():
    from pathlib import Path
    op.get_bind().exec_driver_sql(Path(__file__).with_name('0002_schema.sql').read_text())


def downgrade():
    op.drop_table('memory_deletion_jobs')
    op.drop_table('memory_vectors')
    op.drop_table('memory_candidates')
