import email
import imaplib
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.policy import default
from email.header import decode_header, make_header
from fastapi import HTTPException


def operation(name, call):
    if name == "search":
        # 只读邮件头，不打开附件，不把用户输入作为 IMAP 命令拼接。
        with imaplib.IMAP4_SSL(os.environ['IMAP_HOST'], int(os.environ.get('IMAP_PORT', '993')), ssl_context=ssl.create_default_context()) as mailbox:
            mailbox.login(os.environ['MAIL_USER'], os.environ['MAIL_PASSWORD'])
            mailbox.select('INBOX', readonly=True)
            status, data = mailbox.uid('search', None, 'ALL')
            if status != 'OK':
                raise RuntimeError('IMAP 查询失败')
            results = []
            for identifier in data[0].split()[-20:]:
                status, response = mailbox.uid('fetch', identifier, '(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])')
                if status != 'OK':
                    continue
                raw = next((r[1] for r in response if isinstance(r, tuple)), b'')
                message = email.message_from_bytes(raw, policy=default)
                results.append({'uid':identifier.decode(), 'subject':str(message.get('Subject','')), 'from':str(message.get('From','')), 'date':str(message.get('Date',''))})
            return {'messages': results}
    if name == 'send':
        args = call.arguments
        for field in ('to', 'subject'):
            if not isinstance(args.get(field), str) or '\n' in args[field] or '\r' in args[field]:
                raise HTTPException(422, '邮件字段无效')
        message = EmailMessage()
        message['From'] = os.environ['MAIL_USER']
        message['To'] = args['to']
        message['Subject'] = args['subject']
        message['Message-ID'] = '<' + call.invocation_id + '@homeai.local>'
        message.set_content(str(args.get('text','')))
        with smtplib.SMTP_SSL(os.environ['SMTP_HOST'], int(os.environ.get('SMTP_PORT','465')), context=ssl.create_default_context(), timeout=30) as smtp:
            smtp.login(os.environ['MAIL_USER'], os.environ['MAIL_PASSWORD'])
            refused = smtp.send_message(message)
            if refused:
                raise RuntimeError('部分收件人被拒绝')
        return {'status': 'accepted_by_smtp', 'message_id': message['Message-ID']}
    raise HTTPException(404, '邮件操作不存在')
