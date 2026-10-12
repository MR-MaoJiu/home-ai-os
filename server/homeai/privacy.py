import re
from fastapi import HTTPException

SECRET = re.compile(r"(?i)(?:sk-[a-z0-9_-]{12,}|-----BEGIN .*PRIVATE KEY-----|(?:password|api[_ -]?key|密码|私钥|恢复码)\s*[:：=]\s*\S+)")
PII = re.compile(r"(?P<email>[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})|(?P<phone>(?<!\d)(?:\+86[- ]?)?1[3-9]\d{9}(?!\d))|(?P<id>(?<!\d)\d{17}[\dXx](?!\d))|(?P<card>(?<!\d)\d{16,19}(?!\d))")


def ensure_model_safe(text: str):
    if SECRET.search(text):
        raise HTTPException(422, "检测到凭据或秘密，请使用安全配置入口")


def redact(text: str):
    ensure_model_safe(text)
    mapping = {}
    def replace(match):
        tag = f"<PII_{len(mapping) + 1}>"
        mapping[tag] = match.group(0)
        return tag
    return PII.sub(replace, text), mapping


def validate_search(arguments):
    if set(arguments) != {'query'} or not isinstance(arguments.get('query'), str):
        raise HTTPException(422, '联网搜索只接受 query 文本')
    query = arguments['query'].strip()
    if not query:
        raise HTTPException(422, '搜索词不能为空')
    if len(query) > 500:
        raise HTTPException(422, '搜索词超过 500 个字符，请缩短后重试')
    if any(ord(char) < 32 for char in query):
        raise HTTPException(422, '搜索词包含控制字符，请移除后重试')
    if '!' in query:
        raise HTTPException(422, '搜索词包含不允许的 ! 控制语法，请移除后重试')
    ensure_model_safe(query)
    if match := PII.search(query):
        kind = {'email': '邮箱地址', 'phone': '手机号码', 'id': '身份证号码', 'card': '银行卡号码'}[match.lastgroup]
        raise HTTPException(403, '搜索词包含已识别的' + kind + '，不能发送到搜索引擎')
    return {'query': query}


def validate_mail_send(arguments):
    from email.utils import parseaddr
    if set(arguments) - {'to', 'subject', 'text'}:
        raise HTTPException(422, '邮件参数包含不支持的头部或附件')
    recipient, subject, body = arguments.get('to'), arguments.get('subject'), arguments.get('text', '')
    if not isinstance(recipient, str) or len(recipient) > 254 or any(char in recipient for char in '\r\n,;'):
        raise HTTPException(422, '每封邮件需要一个有效收件地址')
    _, parsed = parseaddr(recipient)
    if parsed != recipient or '@' not in recipient or any(char.isspace() for char in recipient):
        raise HTTPException(422, '收件地址格式无效')
    if not isinstance(subject, str) or not 1 <= len(subject) <= 200 or '\r' in subject or '\n' in subject or not isinstance(body, str) or len(body.encode()) > 100000:
        raise HTTPException(422, '邮件主题或正文格式无效')
    return dict(arguments)
