"""迁移账号与运行账号分离，应用账号不能绕过 RLS。"""
import os
import sys
from pathlib import Path
from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from alembic.config import Config
from alembic import command
from psycopg import sql

root = Path(__file__).resolve().parents[1]
env = dotenv_values(root / ".env.local")
url = f"postgresql+psycopg://homeai_migrator:{env['HOMEAI_DB_ADMIN_PASSWORD']}@127.0.0.1:55432/homeai"
target = 'homeai_test' if '--test' in sys.argv else ('homeai_runtime' if '--runtime' in sys.argv else 'homeai')
if target != 'homeai':
    admin_engine = create_engine(url, isolation_level='AUTOCOMMIT')
    with admin_engine.connect() as connection:
        if not connection.scalar(text('SELECT 1 FROM pg_database WHERE datname=:name'), {'name': target}):
            connection.execute(text('CREATE DATABASE ' + target))
    url = url.removesuffix('/homeai') + '/' + target
os.environ['HOMEAI_DATABASE_URL'] = url
command.upgrade(Config(str(root / "alembic.ini")), "head")
engine = create_engine(url)
with engine.begin() as db:
    if not db.scalar(text("SELECT 1 FROM pg_roles WHERE rolname='homeai_app'")):
        raw = db.connection.driver_connection
        raw.execute(sql.SQL("CREATE ROLE homeai_app LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS").format(sql.Literal(env['HOMEAI_DB_APP_PASSWORD'])))
    db.execute(text("GRANT USAGE ON SCHEMA public TO homeai_app"))
    db.execute(text("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO homeai_app"))
    db.execute(text("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO homeai_app"))
print("数据库迁移完成，应用角色启用 RLS")
