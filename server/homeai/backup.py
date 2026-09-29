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
    removed_media=[]
    with factory() as db:
        from sqlalchemy import select
        for user in db.scalars(select(Principal)).all():
            db.flush();scope(db,user.id,user.household_id)
            db.execute(delete(SyncSnapshot).where(SyncSnapshot.owner_id==user.id))
            db.execute(delete(SyncCursor).where(SyncCursor.owner_id==user.id))
        for line in journal.read_text().splitlines():
            item = vault.open(line, 'deletion-journal')
            user = db.get(Principal, item['owner_id'])
            if not user:
                continue
            db.flush();scope(db, user.id, user.household_id)
            record = db.get(Record, item['record_id'])
            if record:
                from .media import MediaUpload,MediaChunk,MediaJob
                for upload in db.scalars(select(MediaUpload).where(MediaUpload.owner_id==user.id,MediaUpload.record_id==record.id)):
                    removed_media.append(upload.id);upload.status='canceled'
                    db.execute(delete(MediaChunk).where(MediaChunk.owner_id==user.id,MediaChunk.upload_id==upload.id))
                for job in db.scalars(select(MediaJob).where(MediaJob.owner_id==user.id,MediaJob.record_id==record.id)):
                    job.status,job.error='canceled','来源已删除，恢复后不再执行'
                record.deleted, record.payload = True, ''
                db.execute(delete(Revision).where(Revision.record_id == record.id))
                db.execute(delete(Grant).where(Grant.record_id == record.id))
                db.add(Outbox(household_id=user.household_id, owner_id=user.id, kind='record.deleted', resource_id=record.id))
        db.commit()
    return {'removed_media_ids':removed_media}


def replay_memory_forgets(factory,vault,journal:Path):
    if not journal.is_file() or journal.is_symlink():
        raise RuntimeError('缺少独立记忆遗忘日志，禁止开放恢复数据')
    from .auto_memory import replay_forget_journal,MemoryForgetSource,MemoryForgetTombstone
    from .db import Principal,Outbox,scope
    from sqlalchemy import select
    with factory() as db:
        count=replay_forget_journal(db,vault,journal)
        for user in db.scalars(select(Principal)):
            db.flush();scope(db,user.id,user.household_id)
            if db.scalar(select(MemoryForgetTombstone.id).where(MemoryForgetTombstone.owner_id==user.id).limit(1)):
                db.add(Outbox(household_id=user.household_id,owner_id=user.id,kind='record.revoked',resource_id=user.id))
                db.add(Outbox(household_id=user.household_id,owner_id=user.id,kind='memory.updated',resource_id=user.id))
        db.commit()
    return {'replayed_forgets':count}


def prune_restored_media(directory:Path,identifiers):
    import shutil,uuid
    root=directory/'media'
    if root.is_symlink():raise ValueError('恢复媒体目录不能是符号链接')
    for identifier in identifiers:
        if str(uuid.UUID(identifier))!=identifier:raise ValueError('恢复媒体标识无效')
        target=root/identifier
        if target.is_symlink():raise ValueError('恢复媒体对象不能是符号链接')
        if target.is_dir():shutil.rmtree(target)


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


def development_database_name(database_url):
    """仅支持当前仓库开发 Compose 的本机 PostgreSQL，避免误把其他库当作备份源。"""
    import re
    from sqlalchemy.engine import make_url
    try:
        target = make_url(database_url)
    except Exception:
        raise ValueError('数据库地址无效，无法确认备份来源') from None
    if target.get_backend_name() != 'postgresql' or target.host not in {'127.0.0.1', 'localhost'} or target.port != 55432:
        raise ValueError('当前备份仅支持开发 Compose 的本机 PostgreSQL（端口 55432）；其他部署尚不支持，未执行备份')
    if not target.database or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}', target.database):
        raise ValueError('数据库名称无效，无法确认备份来源')
    return target.database


def invalidate_restored_authorizations(factory,vault):
    """仅用于隔离恢复库：旧备份不能重新激活临时资料授权或云端批准。"""
    from sqlalchemy import select
    from .db import Principal,Task,Invocation,Outbox,scope
    from .client_actions import ClientAction,TaskDataGrant,TERMINAL_TASKS
    counts={'revoked_grants':0,'canceled_requests':0,'revoked_cloud_consents':0,'canceled_tasks':0}
    with factory() as db:
        for user in db.scalars(select(Principal)).all():
            db.flush();scope(db,user.id,user.household_id)
            granted_actions=set()
            for grant in db.scalars(select(TaskDataGrant).where(TaskDataGrant.owner_id==user.id)):
                if not grant.revoked:counts['revoked_grants']+=1
                grant.revoked=True;granted_actions.add(grant.action_id)
            for action in db.scalars(select(ClientAction).where(ClientAction.owner_id==user.id)):
                if action.status=='PENDING':
                    action.status='CANCELED';counts['canceled_requests']+=1
                elif action.status=='RESPONDED' and (action.kind=='cloud.disclose' or action.id in granted_actions):
                    action.status='REVOKED'
                    if action.kind=='cloud.disclose':counts['revoked_cloud_consents']+=1
            for task in db.scalars(select(Task).where(Task.owner_id==user.id)):
                payload=vault.open(task.request,user.id+':task:'+task.id)
                if not (payload.get('_task_grants') or payload.get('_cloud_consents') or payload.get('_client_wait')):continue
                task.result=None;task.cancel_requested=True
                if task.status not in TERMINAL_TASKS:
                    task.status='CANCELED';task.error='从备份恢复的临时授权已失效，请重新发起请求';counts['canceled_tasks']+=1
                for invocation in db.scalars(select(Invocation).where(Invocation.owner_id==user.id,Invocation.task_id==task.id)):
                    invocation.result=None
                db.add(Outbox(household_id=user.household_id,owner_id=user.id,kind='record.revoked',resource_id=task.id))
                db.add(Outbox(household_id=user.household_id,owner_id=user.id,kind='task.updated',resource_id=task.id))
        db.commit()
    return counts
