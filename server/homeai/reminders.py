"""提醒时间与通知意图的确定性契约，不依赖模型猜测时区。"""
from datetime import datetime, timezone
from fastapi import HTTPException


def normalize(arguments):
    result = dict(arguments)
    title = result.get('title')
    if not isinstance(title, str) or not title.strip() or len(title) > 500:
        raise HTTPException(422, '提醒标题需要 1 至 500 字符')
    if 'completed' in result and type(result['completed']) is not bool:
        raise HTTPException(422, '提醒完成状态必须为布尔值')
    notify = result.get('notify_at_due', False)
    if type(notify) is not bool:
        raise HTTPException(422, '到期通知选项必须为布尔值')
    due = result.get('due_at')
    if due is not None:
        try:
            if not isinstance(due, str) or len(due) > 50 or 'T' not in due:
                raise ValueError()
            parsed = datetime.fromisoformat(due.replace('Z', '+00:00'))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError()
            result['due_at'] = parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        except (ValueError, OverflowError):
            raise HTTPException(422, '到期时间必须是带时区的 ISO 8601 时间') from None
    elif notify:
        raise HTTPException(422, '到期通知必须指定到期时间')
    return result
