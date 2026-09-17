"""核心数据表与所有者行级隔离。"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # 初始结构使用已冻结的 SQL，避免历史迁移依赖当前 ORM。
    from pathlib import Path
    sql = Path(__file__).with_name("0001_schema.sql").read_text()
    op.get_bind().exec_driver_sql(sql)


def downgrade():
    raise RuntimeError("包含个人数据的初始结构不支持破坏性降级，请恢复加密备份")
