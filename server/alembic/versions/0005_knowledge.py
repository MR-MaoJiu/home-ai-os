"""规范文档的可重建向量分块，不保存第二份明文正文。"""
from alembic import op
import sqlalchemy as sa
revision='0005'
down_revision='0004'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('knowledge_chunks',sa.Column('id',sa.String(),primary_key=True),sa.Column('owner_id',sa.String(),nullable=False),sa.Column('household_id',sa.String(),nullable=False),sa.Column('record_id',sa.String(),nullable=False),sa.Column('version',sa.Integer(),nullable=False),sa.Column('model',sa.String(),nullable=False),sa.Column('position',sa.Integer(),nullable=False),sa.Column('start',sa.Integer(),nullable=False),sa.Column('end',sa.Integer(),nullable=False),sa.UniqueConstraint('record_id','version','model','position'))
    op.create_index('ix_knowledge_chunks_record','knowledge_chunks',['record_id'])
    op.execute('ALTER TABLE knowledge_chunks ADD COLUMN search_vector vector')
    op.execute('ALTER TABLE knowledge_chunks ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE knowledge_chunks FORCE ROW LEVEL SECURITY')
    op.execute("CREATE POLICY owner_access ON knowledge_chunks USING (owner_id=current_setting('homeai.user_id',true) AND household_id=current_setting('homeai.household_id',true)) WITH CHECK (owner_id=current_setting('homeai.user_id',true) AND household_id=current_setting('homeai.household_id',true))")
    op.execute("CREATE POLICY shared_chunks ON knowledge_chunks FOR SELECT USING (household_id=current_setting('homeai.household_id',true) AND record_id IN (SELECT id FROM data_records WHERE NOT deleted AND sensitivity <> 'SECRET'))")


def downgrade():
    op.drop_table('knowledge_chunks')
