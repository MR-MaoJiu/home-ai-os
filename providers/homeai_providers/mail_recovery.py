"""本机邮件账本核对；与真实发送使用同一跨进程锁。"""
import fcntl
import hashlib
import os
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone
from fastapi import HTTPException


@contextmanager
def delivery_lock(subject, invocation):
    directory = Path(os.environ['MAIL_STATE_DIR']) / 'locks'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    identifier = hashlib.sha256((subject + ':' + invocation).encode()).hexdigest()
    descriptor = os.open(directory / identifier, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HTTPException(409, '发送进程仍持有锁，不能核对或重复执行') from None
        yield
    finally:
        os.close(descriptor)


def inspect_delivery(subject, invocation):
    from .mail_adapter import ledger
    if not (Path(os.environ['MAIL_STATE_DIR']) / 'deliveries.sqlite3').is_file():
        raise HTTPException(404, '发送账本不存在，不能凭空创建恢复记录')
    with delivery_lock(subject, invocation):
        db = ledger()
        try:
            row = db.execute('SELECT request_hash,status,message_id,revision FROM deliveries WHERE subject=? AND invocation=?', (subject, invocation)).fetchone()
            if not row:
                raise HTTPException(404, '发送账本不存在该调用')
            return dict(zip(('request_hash', 'status', 'message_id', 'revision'), row))
        finally:
            db.close()


def resolve_delivery(subject, invocation, decision, expected_hash, evidence, expected_revision):
    if decision not in {'COMPLETED', 'NOT_EXECUTED', 'ABORT'} or not isinstance(evidence, str) or not 20 <= len(evidence) <= 20000:
        raise HTTPException(422, '需要有效核对结论与 20 至 20000 字符的实际依据')
    from .mail_adapter import ledger
    if not (Path(os.environ['MAIL_STATE_DIR']) / 'deliveries.sqlite3').is_file():
        raise HTTPException(404, '发送账本不存在，不能凭空创建恢复记录')
    with delivery_lock(subject, invocation):
        db = ledger()
        try:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT request_hash,status,message_id,revision FROM deliveries WHERE subject=? AND invocation=?', (subject, invocation)).fetchone()
            if not row or row[0] != expected_hash or type(expected_revision) is not int or row[3] != expected_revision:
                raise HTTPException(409, '记录不存在、参数摘要或核对版本不符')
            if row[1] not in {'SENDING', 'UNCERTAIN'}:
                raise HTTPException(409, '只能核对不确定记录；不能解锁成功、已处理或已终止记录')
            status = {'COMPLETED': 'MANUAL_ACCEPTED', 'NOT_EXECUTED': 'RETRY_ALLOWED', 'ABORT': 'ABORTED'}[decision]
            db.execute('CREATE TABLE IF NOT EXISTS reconciliations (id INTEGER PRIMARY KEY,subject TEXT NOT NULL,invocation TEXT NOT NULL,decision TEXT NOT NULL,request_hash TEXT NOT NULL,evidence_hash TEXT NOT NULL,created_at TEXT NOT NULL)')
            evidence_hash = hashlib.sha256(evidence.encode()).hexdigest()
            db.execute('INSERT INTO reconciliations(subject,invocation,decision,request_hash,evidence_hash,created_at) VALUES (?,?,?,?,?,?)',
                       (subject, invocation, decision, expected_hash, evidence_hash, datetime.now(timezone.utc).isoformat()))
            db.execute('UPDATE deliveries SET status=?,revision=revision+1 WHERE subject=? AND invocation=?', (status, subject, invocation))
            db.commit()
            return {'status': status, 'message_id': row[2], 'confirmation_source': 'local_operator', 'evidence_hash': evidence_hash}
        finally:
            db.close()
