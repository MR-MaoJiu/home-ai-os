"""加密附件分块上传与服务端解析账本。"""
from alembic import op
import sqlalchemy as sa

revision='0019'
down_revision='0018'
branch_labels=None
depends_on=None


def owned():
    return [sa.Column('id',sa.String(),primary_key=True),sa.Column('owner_id',sa.String(),nullable=False),sa.Column('household_id',sa.String(),nullable=False)]


def upgrade():
    op.create_table('media_uploads',*owned(),sa.Column('client_id',sa.String(),nullable=False),sa.Column('request_hash',sa.String(),nullable=False),
        sa.Column('device_id',sa.String(),nullable=False),sa.Column('kind',sa.String(),nullable=False),sa.Column('metadata_json',sa.Text(),nullable=False),
        sa.Column('size',sa.Integer(),nullable=False),sa.Column('sha256',sa.String(),nullable=False),sa.Column('status',sa.String(),nullable=False),
        sa.Column('created_at',sa.Float(),nullable=False),sa.Column('expires_at',sa.Float(),nullable=False),sa.Column('record_id',sa.String()),sa.UniqueConstraint('owner_id','client_id'))
    op.create_table('media_chunks',*owned(),sa.Column('upload_id',sa.String(),nullable=False),sa.Column('position',sa.Integer(),nullable=False),
        sa.Column('size',sa.Integer(),nullable=False),sa.Column('sha256',sa.String(),nullable=False),sa.UniqueConstraint('upload_id','position'))
    op.create_table('media_jobs',*owned(),sa.Column('record_id',sa.String(),nullable=False,unique=True),sa.Column('upload_id',sa.String(),nullable=False),
        sa.Column('device_id',sa.String(),nullable=False),sa.Column('version',sa.Integer(),nullable=False),sa.Column('status',sa.String(),nullable=False),
        sa.Column('attempts',sa.Integer(),nullable=False),sa.Column('retry_at',sa.Float(),nullable=False),sa.Column('lease_until',sa.Float(),nullable=False),
        sa.Column('error',sa.String()),sa.Column('result_record_id',sa.String()))
    for table,column in (('media_uploads','record_id'),('media_chunks','upload_id')):op.create_index('ix_'+table+'_'+column,table,[column])
    for table in ('media_uploads','media_chunks','media_jobs'):
        for column in ('owner_id','household_id'):op.create_index('ix_'+table+'_'+column,table,[column])
        op.execute('ALTER TABLE '+table+' ENABLE ROW LEVEL SECURITY')
        op.execute('ALTER TABLE '+table+' FORCE ROW LEVEL SECURITY')
        predicate="owner_id=current_setting('homeai.user_id',true) AND household_id=current_setting('homeai.household_id',true)"
        op.execute('CREATE POLICY owner_isolation ON '+table+' USING ('+predicate+') WITH CHECK ('+predicate+')')


def downgrade():
    for table in ('media_jobs','media_chunks','media_uploads'):op.drop_table(table)
