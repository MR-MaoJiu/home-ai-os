"""恢复到一个全新、隔离的数据库；绝不覆盖当前业务库。"""
import argparse
import io
import os
import re
import subprocess
import tempfile
from pathlib import Path
from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from homeai.backup import decrypt_stream,safe_extract,replay_deletions,verify_archive
from homeai.crypto import Vault
from homeai.db import database

root=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser()
p.add_argument('--file',type=Path,required=True)
p.add_argument('--key',type=Path,required=True)
p.add_argument('--database',required=True)
p.add_argument('--deletion-journal',type=Path,required=True,help='备份之外独立保管的最新删除日志')
p.add_argument('--allow-legacy',action='store_true',help='显式接受缺少完整性清单的旧备份')
a=p.parse_args()
if not re.fullmatch(r'homeai_restore_[a-z0-9_]+',a.database):raise SystemExit('目标必须为新的 homeai_restore_ 数据库')
if not a.deletion_journal.is_file():raise SystemExit('独立删除日志缺失，不能恢复')
config=dotenv_values(root/'.env.local')
admin=f"postgresql+psycopg://homeai_migrator:{config['HOMEAI_DB_ADMIN_PASSWORD']}@127.0.0.1:55432/homeai"
key=a.key.read_bytes()
if len(key)!=32 or a.key.stat().st_mode & 0o077:raise SystemExit('备份密钥格式/权限无效')
buffer=io.BytesIO()
with a.file.open('rb') as src:decrypt_stream(src,buffer,key)
verify_archive(buffer,allow_legacy=a.allow_legacy)
buffer.seek(0)
engine=create_engine(admin,isolation_level='AUTOCOMMIT')
with engine.connect() as db:
    if db.scalar(text('SELECT 1 FROM pg_database WHERE datname=:name'),{'name':a.database}):raise SystemExit('目标库已存在，禁止覆盖')
    db.execute(text('CREATE DATABASE '+a.database))
# 解密临时目录受 0700 保护，所有内容原本还有应用层加密。
with tempfile.TemporaryDirectory(prefix='homeai-restore-') as directory:
    destination=Path(directory)
    safe_extract(buffer,destination)
    result=subprocess.run(['docker','compose','--env-file',str(root/'.env.local'),'-f',str(root/'deploy/compose.dev.yml'),'exec','-T','postgres','pg_restore','-U','homeai_migrator','--no-owner','--exit-on-error','-d',a.database],input=(destination/'database.dump').read_bytes(),capture_output=True)
    if result.returncode:raise SystemExit('数据库恢复失败，隔离库保留供排查；未开放访问')
    target=make_url(admin).set(database=a.database)
    # 旧备份先升级到当前模式，才能清除新版本同步缓存并重放删除日志。
    from alembic.config import Config
    from alembic import command
    target_url=target.render_as_string(hide_password=False)
    previous_url=os.environ.get('HOMEAI_DATABASE_URL')
    os.environ['HOMEAI_DATABASE_URL']=target_url
    try:command.upgrade(Config(str(root/'alembic.ini')),'head')
    finally:
        if previous_url is None:os.environ.pop('HOMEAI_DATABASE_URL',None)
        else:os.environ['HOMEAI_DATABASE_URL']=previous_url
    _,factory=database(target_url)
    with factory() as db:
        db.execute(text('GRANT USAGE ON SCHEMA public TO homeai_app'))
        db.execute(text('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO homeai_app'))
        db.execute(text('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO homeai_app'))
        db.commit()
    replay_deletions(factory,Vault.from_file(root/'state/master.key'),a.deletion_journal)
    output=root/'state/restores'/a.database
    output.mkdir(parents=True,exist_ok=False)
    os.chmod(output,0o700)
    if (destination/'blobs').exists():
        import shutil
        shutil.copytree(destination/'blobs',output/'blobs')
    if (destination/'server-identity.enc').exists():
        # 身份仍为主密钥加密的密文；只放入隔离恢复目录，不覆盖正在使用的身份。
        import shutil
        shutil.copyfile(destination/'server-identity.enc',output/'server-identity.enc')
        os.chmod(output/'server-identity.enc',0o600)
    # 删除日志同时应用到恢复出的对象，禁止物理附件残留。
    vault=Vault.from_file(root/'state/master.key')
    for line in a.deletion_journal.read_text().splitlines():
        tombstone=vault.open(line,'deletion-journal')
        (output/'blobs'/tombstone['record_id']).unlink(missing_ok=True)
print('已恢复到隔离数据库 '+a.database+'，删除日志已重放；尚未切换业务服务。')
