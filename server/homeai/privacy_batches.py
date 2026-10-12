"""本地检测容量降级：完整覆盖原文，仅对确定的容量拒绝分批重试。"""
import httpx


MAX_CALLS = 31
MAX_DEPTH = 5
OVERLAP_CHARACTERS = 500
MIN_SPLIT_CHARACTERS = OVERLAP_CHARACTERS * 2
CAPACITY_CODES = frozenset({
    'exceed_context_size_error', 'context_length_exceeded',
    'context_window_exceeded', 'max_context_length_exceeded',
    'context_too_large', 'prompt_too_long', 'request_too_large',
})


class CapacityLimit(Exception):
    """无法在受限的检测预算内完整检查，调用方必须暂停出站。"""


def error_codes(exc):
    """只取固定机器码进行分支；错误正文可能含输入，不对外回显。"""
    if not isinstance(exc, httpx.HTTPStatusError):
        return set()
    response = exc.response
    if len(response.content) > 16384:
        return set()
    try:
        body = response.json()
    except (ValueError, UnicodeError):
        return set()
    error = body.get('error') if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return set()
    return {value for key in ('type', 'code')
            if isinstance(value := error.get(key), str) and len(value) <= 100}


def is_capacity_error(exc):
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    return (exc.response.status_code == 413 or
            exc.response.status_code in {400, 422} and bool(error_codes(exc) & CAPACITY_CODES))


def http_failure(exc):
    """固定分类避免把服务故障说成内容敏感，也不泄露上游原文。"""
    status = exc.response.status_code
    if is_capacity_error(exc):
        return ('privacy_context_too_large', '本次内容超过本地隐私模型可处理的容量',
                '请缩小本次资料范围，或在管理端使用上下文容量更大的本地隐私模型后重试。')
    if status in {400, 422} and error_codes(exc) & {
        'unsupported_response_format', 'unsupported_json_schema', 'invalid_json_schema',
    }:
        return ('privacy_format_unsupported', '本地隐私模型不支持本次结构化检测格式',
                '请在管理端检查本地隐私模型的结构化输出能力后重试。')
    if status in {400, 422}:
        return ('privacy_request_rejected', '本地隐私模型拒绝了本次检测请求',
                '请在管理端检查本地隐私模型的请求兼容性后重试。')
    if status in {401, 403}:
        return ('privacy_auth_failed', '本地隐私模型的访问认证失败',
                '请在管理端检查本地隐私模型凭据后重试。')
    if status == 404:
        return ('privacy_endpoint_missing', '本地隐私模型接口不存在',
                '请在管理端检查本地隐私模型接口地址后重试。')
    if status == 429:
        return ('privacy_busy', '本地隐私模型当前繁忙', '请稍后重新检查本次内容。')
    if status >= 500:
        return ('privacy_service_error', '本地隐私模型服务异常',
                '请恢复本地隐私模型服务后重试。')
    return ('privacy_unavailable', '本地隐私模型服务返回失败',
            '请检查本地隐私模型服务后重试。')


async def inspect_complete(text, inspect, *, review=False):
    """每个叶子完整验证后才合并；任何错误使整轮失败，不返回部分成功。"""
    calls = 0

    async def visit(chunk, depth):
        nonlocal calls
        if calls >= MAX_CALLS:
            raise CapacityLimit()
        calls += 1
        try:
            return await inspect(chunk)
        except httpx.HTTPStatusError as exc:
            if not is_capacity_error(exc):
                raise
            if depth >= MAX_DEPTH or len(chunk) <= MIN_SPLIT_CHARACTERS:
                raise CapacityLimit() from None
        # 两个子块均严格缩小；重叠覆盖最长500字符实体的全部切分位置。
        middle = len(chunk) // 2
        left_end = middle + OVERLAP_CHARACTERS // 2
        right_start = left_end - OVERLAP_CHARACTERS
        left = await visit(chunk[:left_end], depth + 1)
        right = await visit(chunk[right_start:], depth + 1)
        categories = list(dict.fromkeys(left.get('reason_codes', []) + right.get('reason_codes', [])))
        if review:
            return {'safe': True, 'uncertain': False, 'reason_codes': categories}
        return {'entities': list(dict.fromkeys(left['entities'] + right['entities'])),
                'sensitive': left['sensitive'] or right['sensitive'],
                'uncertain': False, 'reason_codes': categories}

    return await visit(text, 0)
