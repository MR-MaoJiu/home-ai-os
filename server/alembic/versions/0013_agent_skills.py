"""家庭管理员发布的指令 Skill，工具权限仍由 Core 控制。"""
from alembic import op
import sqlalchemy as sa
revision='0013'
down_revision='0012'
branch_labels=None
depends_on=None

def upgrade():
    op.create_table('agent_skills',sa.Column('id',sa.String(),primary_key=True),sa.Column('household_id',sa.String(),nullable=False),sa.Column('owner_id',sa.String(),nullable=False),sa.Column('name',sa.String(),nullable=False),sa.Column('description',sa.Text(),nullable=False),sa.Column('content',sa.Text(),nullable=False),sa.Column('enabled',sa.Boolean(),nullable=False))
    for column in ('owner_id','household_id'):op.create_index('ix_agent_skills_'+column,'agent_skills',[column])
    op.execute('ALTER TABLE agent_skills ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE agent_skills FORCE ROW LEVEL SECURITY')
    op.execute("CREATE POLICY household_read ON agent_skills FOR SELECT USING (household_id=current_setting('homeai.household_id',true))")
    for action in ('INSERT','UPDATE','DELETE'):
        condition="owner_id=current_setting('homeai.user_id',true) AND household_id=current_setting('homeai.household_id',true)"
        clause=(' USING ('+condition+')' if action!='INSERT' else '')+(' WITH CHECK ('+condition+')' if action!='DELETE' else '')
        op.execute('CREATE POLICY owner_'+action.lower()+' ON agent_skills FOR '+action+clause)

def downgrade():op.drop_table('agent_skills')
