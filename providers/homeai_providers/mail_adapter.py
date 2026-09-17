"""成员绑定的 IMAP/SMTP 适配，强制 TLS，发送状态单独持久化。"""
import email
import hashlib
import imaplib
import json
import os
import smtplib
import sqlite3
import ssl
from pathlib import Path
from email.message import EmailMessage
from email.policy import default
from email.utils import parseaddr
from fastapi import HTTPException


def tls_context():
    return ssl.create_default_context(cafile=os.environ.get('MAIL_CA_FILE') or None)


def address(value):
    if not isinstance(value, str) or len(value) > 254 or any(char in value for char in '\r\n,;'):
        raise HTTPException(422, '每封邮件只允许一个有效收件地址')
    _, parsed = parseaddr(value)
    if parsed != value or '@' not in parsed or any(char.isspace() for char in parsed):
        raise HTTPException(422, '邮箱地址格式无效')
    return parsed


def smtp_connection():
    mode = os.environ.get('SMTP_SECURITY', 'tls')
    if mode == 'tls':
        return smtplib.SMTP_SSL(os.environ['SMTP_HOST'], int(os.environ.get('SMTP_PORT', '465')), context=tls_context(), timeout=20)
    if mode != 'starttls':
        raise HTTPException(503, 'SMTP 必须使用 TLS 或 STARTTLS')
    smtp = smtplib.SMTP(os.environ['SMTP_HOST'], int(os.environ.get('SMTP_PORT', '587')), timeout=20)
    try:
        smtp.ehlo()
        smtp.starttls(context=tls_context())
        smtp.ehlo()
        return smtp
    except Exception:
        smtp.close()
        raise


def imap_connection():
    mode = os.environ.get('IMAP_SECURITY', 'tls')
    if mode == 'tls':
        return imaplib.IMAP4_SSL(os.environ['IMAP_HOST'], int(os.environ.get('IMAP_PORT', '993')), ssl_context=tls_context(), timeout=20)
    if mode != 'starttls':
        raise HTTPException(503, 'IMAP 必须使用 TLS 或 STARTTLS')
    mailbox = imaplib.IMAP4(os.environ['IMAP_HOST'], int(os.environ.get('IMAP_PORT', '143')), timeout=20)
    try:
        mailbox.starttls(ssl_context=tls_context())
        return mailbox
    except Exception:
        mailbox.shutdown()
        raise


def ledger():
    path = Path(os.environ['MAIL_STATE_DIR']) / 'deliveries.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise HTTPException(503, '发送账本不能是符号链接')
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    if path.stat().st_mode & 0o077:
        raise HTTPException(503, '发送账本权限必须为 0600')
    db = sqlite3.connect(path, timeout=10)
    db.execute('BEGIN IMMEDIATE')
    db.execute('CREATE TABLE IF NOT EXISTS deliveries (subject TEXT, invocation TEXT, request_hash TEXT NOT NULL, status TEXT NOT NULL, message_id TEXT NOT NULL, PRIMARY KEY(subject,invocation))')
    if 'revision' not in {row[1] for row in db.execute('PRAGMA table_info(deliveries)')}:
        db.execute('ALTER TABLE deliveries ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')
    db.commit()
    return db


def send(call):
    args = call.arguments
    if set(args) - {'to', 'subject', 'text'}:
        raise HTTPException(422, '不支持额外邮件头、附件或任意发送选项')
    recipient = address(args.get('to'))
    sender = address(os.environ.get('MAIL_FROM', os.environ['MAIL_USER']))
    subject, body = args.get('subject'), args.get('text', '')
    if not isinstance(subject, str) or not 1 <= len(subject) <= 200 or '\r' in subject or '\n' in subject or not isinstance(body, str) or len(body.encode()) > 100000:
        raise HTTPException(422, '邮件主题或正文长度/格式无效')
    payload = {'to': recipient, 'from': sender, 'subject': subject, 'text': body}
    request_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    identifier = hashlib.sha256((call.subject_id + ':' + call.invocation_id).encode()).hexdigest()
    message_id = '<' + identifier + '@homeai.local>'
    from .mail_recovery import delivery_lock
    with delivery_lock(call.subject_id, call.invocation_id):
        return send_locked(call, payload, request_hash, message_id)


def send_locked(call, payload, request_hash, message_id):
    sender, recipient, subject, body = (payload[key] for key in ('from', 'to', 'subject', 'text'))
    db = ledger()
    try:
        db.execute('BEGIN IMMEDIATE')
        prior = db.execute('SELECT request_hash,status,message_id FROM deliveries WHERE subject=? AND invocation=?', (call.subject_id, call.invocation_id)).fetchone()
        if prior:
            if prior[0] != request_hash:
                raise HTTPException(409, '发送标识已绑定其他参数')
            if prior[1] in {'ACCEPTED', 'MANUAL_ACCEPTED'}:
                db.rollback()
                return {'status': 'accepted_by_smtp' if prior[1] == 'ACCEPTED' else 'confirmed_by_operator',
                        'message_id': prior[2], 'deduplicated': True,
                        'confirmation_source': 'smtp_response' if prior[1] == 'ACCEPTED' else 'local_operator'}
            if prior[1] != 'RETRY_ALLOWED':
                raise HTTPException(409, '之前发送结果不明，禁止自动重复发送，请在邮件系统核对')
            db.execute('UPDATE deliveries SET status=?,revision=revision+1 WHERE subject=? AND invocation=?', ('SENDING', call.subject_id, call.invocation_id))
        else:
            db.execute('INSERT INTO deliveries(subject,invocation,request_hash,status,message_id) VALUES (?,?,?,?,?)', (call.subject_id, call.invocation_id, request_hash, 'SENDING', message_id))
        db.commit()
        message = EmailMessage()
        message['From'], message['To'], message['Subject'], message['Message-ID'] = sender, recipient, subject, message_id
        message.set_content(body)
        smtp = None
        try:
            smtp = smtp_connection()
            smtp.login(os.environ['MAIL_USER'], os.environ['MAIL_PASSWORD'])
            refused = smtp.send_message(message, from_addr=sender, to_addrs=[recipient])
            if refused:
                raise RuntimeError('SMTP 拒绝收件人')
            db.execute('UPDATE deliveries SET status=?,revision=revision+1 WHERE subject=? AND invocation=?', ('ACCEPTED', call.subject_id, call.invocation_id))
            db.commit()
        except Exception:
            db.execute('UPDATE deliveries SET status=?,revision=revision+1 WHERE subject=? AND invocation=?', ('UNCERTAIN', call.subject_id, call.invocation_id))
            db.commit()
            raise
        finally:
            if smtp:
                smtp.close()
        return {'status': 'accepted_by_smtp', 'message_id': message_id, 'deduplicated': False}
    finally:
        db.close()


def search(call):
    args = call.arguments
    if set(args) - {'query', 'limit', 'before_uid', 'uidvalidity'}:
        raise HTTPException(422, '邮件查询参数无效')
    query, limit = args.get('query', ''), args.get('limit', 20)
    before = args.get('before_uid')
    if not isinstance(query, str) or len(query) > 200 or type(limit) is not int or not 1 <= limit <= 50 or (before is not None and (type(before) is not int or before < 1)):
        raise HTTPException(422, '邮件查询范围无效')
    with imap_connection() as mailbox:
        mailbox.login(os.environ['MAIL_USER'], os.environ['MAIL_PASSWORD'])
        status, _ = mailbox.select('INBOX', readonly=True)
        if status != 'OK':
            raise RuntimeError('不能打开收件箱')
        validity = int(mailbox.response('UIDVALIDITY')[1][0])
        next_uid = int(mailbox.response('UIDNEXT')[1][0])
        if 'uidvalidity' in args and (type(args['uidvalidity']) is not int or args['uidvalidity'] != validity):
            raise HTTPException(409, '邮箱 UIDVALIDITY 已变化，请重新开始分页')
        high = min(before or next_uid, next_uid) - 1
        low = max(1, high - 199)
        if high < 1:
            return {'messages': [], 'uidvalidity': validity, 'next_before_uid': None, 'has_more': False}
        status, data = mailbox.uid('search', None, 'UID', f'{low}:{high}')
        if status != 'OK':
            raise RuntimeError('IMAP 查询失败')
        messages, cursor = [], low
        for identifier in reversed(data[0].split()):
            cursor = int(identifier)
            status, response = mailbox.uid('fetch', identifier, '(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])')
            if status != 'OK':
                raise RuntimeError('IMAP 邮件头读取失败')
            raw = next((item[1] for item in response if isinstance(item, tuple)), b'')
            if len(raw) > 65536:
                raise HTTPException(502, '邮件头超过安全长度限制')
            message = email.message_from_bytes(raw, policy=default)
            record = {'uid': cursor, 'subject': str(message.get('Subject', ''))[:2000],
                      'from': str(message.get('From', ''))[:2000], 'date': str(message.get('Date', ''))[:200],
                      'message_id': str(message.get('Message-ID', ''))[:500]}
            if query.casefold() in (record['subject'] + ' ' + record['from']).casefold():
                messages.append(record)
            if len(messages) >= limit:
                break
        else:
            cursor = low
        return {'messages': messages, 'uidvalidity': validity, 'next_before_uid': cursor if cursor > 1 else None,
                'has_more': cursor > 1, 'scope': 'inbox_headers'}



def read_message(call):
    args = call.arguments
    if set(args) != {'uid', 'uidvalidity'} or any(type(args[key]) is not int or args[key] < 1 for key in args):
        raise HTTPException(422, '读取邮件需要有效 UID 和 UIDVALIDITY')
    with imap_connection() as mailbox:
        mailbox.login(os.environ['MAIL_USER'], os.environ['MAIL_PASSWORD'])
        if mailbox.select('INBOX', readonly=True)[0] != 'OK':
            raise RuntimeError('不能打开收件箱')
        validity = int(mailbox.response('UIDVALIDITY')[1][0])
        if validity != args['uidvalidity']:
            raise HTTPException(409, '邮箱标识已变化，请重新查询')
        identifier = str(args['uid'])
        status, sizes = mailbox.uid('fetch', identifier, '(RFC822.SIZE)')
        import re
        match = re.search(rb'RFC822.SIZE (\d+)', b' '.join(item for item in sizes if isinstance(item, bytes)))
        if status != 'OK' or not match:
            raise HTTPException(404, '邮件不存在')
        if int(match.group(1)) > 1024 * 1024:
            raise HTTPException(413, '邮件超过 1 MiB，请在邮件客户端查看')
        status, response = mailbox.uid('fetch', identifier, '(BODY.PEEK[])')
        if status != 'OK':
            raise RuntimeError('邮件读取失败')
        raw = next((item[1] for item in response if isinstance(item, tuple)), b'')
        if not raw or len(raw) > 1024 * 1024:
            raise HTTPException(502, '邮件内容无效或超过大小限制')
        message = email.message_from_bytes(raw, policy=default)
        body = message.get_body(preferencelist=('plain',))
        content = body.get_content() if body else None
        if content is not None and (not isinstance(content, str) or len(content) > 100000):
            raise HTTPException(413, '邮件正文超过限制')
        account = hashlib.sha256((os.environ['IMAP_HOST'] + ':' + os.environ['MAIL_USER']).encode()).hexdigest()
        return {'uid': args['uid'], 'uidvalidity': validity, 'source_key': account + ':' + str(validity) + ':' + identifier,
                'subject': str(message.get('Subject', '')), 'from': str(message.get('From', '')),
                'date': str(message.get('Date', '')), 'message_id': str(message.get('Message-ID', '')),
                'text': content, 'body_status': 'plain_text' if body else 'no_plain_text_part',
                'attachments': [{'filename': str(part.get_filename() or '')[:500], 'content_type': part.get_content_type()}
                                for part in message.iter_attachments()], 'content_trust': 'untrusted_mail'}


def operation(name, call):
    subject = os.environ.get('MAIL_SUBJECT_ID')
    if not subject or call.subject_id != subject:
        raise HTTPException(403, '此邮件账户未授权给当前成员')
    if name == 'search':
        return search(call)
    if name == 'send':
        return send(call)
    if name == 'read':
        return read_message(call)
    raise HTTPException(404, '邮件操作不存在')
