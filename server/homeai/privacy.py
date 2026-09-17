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


def cloud_context(records):
    # 正则无法可靠识别人名、地址和任意附件。未配置并验证 NER/本地复核链时只允许公开记录。
    if any(r.sensitivity != "PUBLIC" or r.cloud_policy != "REDACT_AND_ALLOW" for r in records):
        raise HTTPException(403, "私人内容的完整脱敏链尚未通过验收，禁止上云")
