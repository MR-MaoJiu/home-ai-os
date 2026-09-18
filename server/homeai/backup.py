"""加密备份与安全展开；恢复数据库必须在维护窗口显式执行。"""
import io
import json
import os
import struct
import tarfile
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b'HOMEAI-BACKUP-1\n'
CHUNK = 1024 * 1024


def encrypt_stream(source, destination, key):
    cipher = AESGCM(key)
    prefix = os.urandom(8)
    destination.write(MAGIC + prefix)
    index = 0
    while True:
        raw = source.read(CHUNK)
        encrypted = cipher.encrypt(prefix + struct.pack('>I', index), raw, MAGIC)
        destination.write(struct.pack('>I', len(encrypted)) + encrypted)
        index += 1
        if not raw:
            break


def decrypt_stream(source, destination, key):
    if source.read(len(MAGIC)) != MAGIC:
        raise ValueError('备份格式无效')
    prefix = source.read(8)
    cipher = AESGCM(key)
    index = 0
    while True:
        size = source.read(4)
        if len(size) != 4:
            raise ValueError('备份被截断')
        count = struct.unpack('>I', size)[0]
        if not 16 <= count <= CHUNK + 16:
            raise ValueError('备份分块无效')
        raw = cipher.decrypt(prefix + struct.pack('>I', index), source.read(count), MAGIC)
        index += 1
        if not raw:
            if source.read(1):
                raise ValueError('备份末尾有额外内容')
            return
        destination.write(raw)


def safe_extract(archive, destination: Path):
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(fileobj=archive, mode='r:*') as tar:
        for member in tar.getmembers():
            path = (root / member.name).resolve()
            if not path.is_relative_to(root) or member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                raise ValueError('备份包含不安全路径')
        tar.extractall(root, filter='data')


def replay_deletions(factory, vault, journal: Path):
    from sqlalchemy import delete
    from .db import Record, Revision, Grant, Principal, Outbox, SyncSnapshot, SyncCursor, scope
    if not journal.exists():
        raise RuntimeError('缺少独立删除日志，禁止开放恢复数据')
    with factory() as db:
        from sqlalchemy import select
        for user in db.scalars(select(Principal)).all():
            scope(db,user.id,user.household_id)
            db.execute(delete(SyncSnapshot).where(SyncSnapshot.owner_id==user.id))
            db.execute(delete(SyncCursor).where(SyncCursor.owner_id==user.id))
        for line in journal.read_text().splitlines():
            item = vault.open(line, 'deletion-journal')
            user = db.get(Principal, item['owner_id'])
            if not user:
                continue
            scope(db, user.id, user.household_id)
            record = db.get(Record, item['record_id'])
            if record:
                record.deleted, record.payload = True, ''
                db.execute(delete(Revision).where(Revision.record_id == record.id))
                db.execute(delete(Grant).where(Grant.record_id == record.id))
                db.add(Outbox(household_id=user.household_id, owner_id=user.id, kind='record.deleted', resource_id=record.id))
        db.commit()


def verify_archive(archive, allow_legacy=False):
    """逐文件核对，不允许重复路径、链接或未列入清单的文件。"""
    import hashlib
    from pathlib import PurePosixPath
    archive.seek(0)
    with tarfile.open(fileobj=archive, mode='r:*') as tar:
        members = tar.getmembers()
        if len(members) > 100000:
            raise ValueError('归档条目过多')
        seen, actual = set(), {}
        for member in members:
            path = PurePosixPath(member.name)
            if not member.name or str(path) == '.' or path.is_absolute() or '..' in path.parts or str(path) in seen or not (member.isfile() or member.isdir()):
                raise ValueError('归档包含重复或不安全路径')
            seen.add(str(path))
            if member.isfile() and member.name != 'manifest.json':
                source = tar.extractfile(member)
                if source is None:
                    raise ValueError('不能读取归档文件')
                actual[member.name] = {'bytes': member.size, 'sha256': hashlib.file_digest(source, 'sha256').hexdigest()}
        if 'database.dump' not in actual:
            raise ValueError('归档缺少数据库文件')
        try:
            manifest_entry = tar.getmember('manifest.json')
        except KeyError:
            if not allow_legacy:
                raise ValueError('旧备份没有完整性清单，必须显式允许旧格式') from None
            return {'format_version': 1, 'files': len(actual), 'manifest_verified': False}
        if not manifest_entry.isfile() or manifest_entry.size > 16 * 1024 * 1024:
            raise ValueError('备份清单无效或过大')
        manifest = json.load(tar.extractfile(manifest_entry))
        if manifest.get('format_version') != 2 or manifest.get('files') != actual:
            raise ValueError('归档文件与完整性清单不一致')
        return {'format_version': 2, 'files': len(actual), 'manifest_verified': True}
