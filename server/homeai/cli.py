import argparse
import json
import os
from pathlib import Path
from sqlalchemy import select
from .config import Settings
from .db import database, Principal, uid
from .security import credential


def main():
    parser = argparse.ArgumentParser(description="Home AI OS 本机管理工具")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-key")
    bootstrap = commands.add_parser("bootstrap")
    bootstrap.add_argument("--name", required=True)
    recover = commands.add_parser("web-recover")
    recover.add_argument("--user", required=True)
    web = commands.add_parser("web-setup")
    web.add_argument("--user", required=True)
    pair = commands.add_parser("pair")
    pair.add_argument("--user", required=True)
    pair.add_argument("--url", help="生成包含稳定服务器身份的第二版配对信息")
    pair.add_argument("--output", type=Path, help="将第二版配对信息写入新的 0600 文件")
    member = commands.add_parser("member")
    member.add_argument("--household", required=True)
    member.add_argument("--name", required=True)
    args = parser.parse_args()
    if args.command == "pair" and args.output and not args.url:parser.error("--output 必须与 --url 一起使用")
    if args.command == "pair" and args.url:
        from .server_identity import addresses
        addresses([args.url])
    settings = Settings()
    if args.command == "init-key":
        settings.master_key_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(settings.master_key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(os.urandom(32))
        print("主密钥已创建；请建立独立安全备份。")
        return
    _, factory = database(settings.database_url)
    with factory() as db:
        if args.command == "bootstrap":
            # PostgreSQL 事务锁避免并发创建两个基础设施所有者。
            from sqlalchemy import text
            db.execute(text("SELECT pg_advisory_xact_lock(804219)"))
            if db.scalar(select(Principal).limit(1)):
                raise SystemExit("系统已初始化，禁止再次引导")
            user = Principal(id=uid(), household_id=uid(), name=args.name, role="infrastructure_owner")
            db.add(user)
            db.flush()
        elif args.command == "member":
            if not db.scalar(select(Principal).where(Principal.household_id == args.household)):
                raise SystemExit("家庭不存在")
            user = Principal(id=uid(), household_id=args.household, name=args.name, role="adult")
            db.add(user)
            db.flush()
        else:
            user = db.get(Principal, args.user)
            if not user:
                raise SystemExit("用户不存在")
        token = credential(db, user.id, "browser_recovery" if args.command == "web-recover" else ("browser_bootstrap" if args.command == "web-setup" else "pair"), 300)
        db.commit()
        # 配对码为短期一次性凭据，仅显示于明确调用的本机终端。
        if args.command == 'pair' and args.url:
            from types import SimpleNamespace
            from .crypto import Vault
            from .server_identity import public_identity,addresses
            public=public_identity(SimpleNamespace(settings=settings,vault=Vault.from_file(settings.master_key_file)))
            urls=addresses([args.url,*public['addresses']])
            payload={'schema_version':'2.0',**public,'url':urls[0],'addresses':urls,'token':token}
            encoded=json.dumps(payload,ensure_ascii=False)
            if args.output:
                with os.fdopen(os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as file:file.write(encoded)
                print('第二版配对信息已写入指定私有文件，有效期 5 分钟。')
            else:print(encoded)
        else:
            print(json.dumps({"user_id": user.id, "household_id": user.household_id, "pairing_token": token, "expires_in": 300}, ensure_ascii=False))
