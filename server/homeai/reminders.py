"""提醒时间与通知意图的确定性契约，不依赖模型猜测时区。"""
from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo
from fastapi import HTTPException


class ReminderSubjectMissing(HTTPException):
    def __init__(self):
        super().__init__(422, '用户已经说明时间但没有说明提醒事项；调用ask_user只询问需要提醒做什么，不要把时间本身当作提醒标题')


def _number(value):
    if value == '半':
        return 0.5
    if re.fullmatch(r'\d+(?:\.\d+)?', value):
        return float(value)
    digits = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
              '六': 6, '七': 7, '八': 8, '九': 9}
    if '十' in value:
        head, tail = value.split('十', 1)
        return digits.get(head, 1) * 10 + digits.get(tail, 0) if len(head) <= 1 and len(tail) <= 1 else None
    return digits.get(value)


def explicit_due_at(message, created_at, zone):
    """只解析单个明确时间，避免模型二次换算时区；基准始终是原任务接收时刻。"""
    from .task_goals import single_reminder
    if not single_reminder(message) or planned_reminder_requirement(message) != 'scheduled':
        return None
    if re.search(r'或者|还是|或是|改成|改为|左右|大概|差不多', message):
        return None
    reference = datetime.fromtimestamp(created_at, ZoneInfo(zone))
    numeral = r'(?:\d+(?:\.\d+)?|[零一二三四五六七八九十两半]+)'
    clocks = re.findall(numeral + r'(?:[:：]\d{2}|点)', message)
    durations = list(re.finditer(r'(' + numeral + r')\s*(分钟|小时|秒钟|秒)\s*(?:之)?后', message))
    # “1小时30分钟后”不是单个时长，不能只截取末尾30分钟；混合日期也不推算。
    time_units = re.findall(numeral + r'\s*(?:分钟|小时|秒钟|秒)', message)
    if durations and (clocks or len(time_units) != 1 or re.search(r'今天|明天|后天|今晚|明早', message)):
        return None
    if len(durations) == 1:
        amount = _number(durations[0][1])
        if amount is not None and 0 < amount <= 10000:
            seconds = amount * {'分钟': 60, '小时': 3600, '秒钟': 1, '秒': 1}[durations[0][2]]
            return datetime.fromtimestamp(created_at + seconds, timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    if durations:
        return None
    if len(clocks) != 1 or len(re.findall(r'今天|明天|后天|今晚|明早', message)) != 1:
        return None
    pattern = (r'(今天|明天|后天|今晚|明早)\s*(凌晨|早上|早晨|上午|中午|下午|傍晚|晚上)?\s*('
        + numeral + r')(?:[:：](\d{2})|点(?:([零一二三四五六七八九十两\d]+)分|半|钟)?)')
    matches = list(re.finditer(pattern, message))
    if len(matches) != 1:
        return None
    match = matches[0]
    day, period = match[1], match[2] or ({'今晚': '晚上', '明早': '上午'}.get(match[1]))
    hour = _number(match[3])
    minute = _number(match[4] or match[5]) if match[4] or match[5] else 30 if match[0].endswith('半') else 0
    if hour is None or minute is None or hour % 1 or minute % 1 or not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    # 没有上午/下午的“八点”仍有歧义，交给补充流程，不擅自替用户选时段。
    if not period and hour < 13 and match[4] is None:
        return None
    if period in {'下午', '傍晚', '晚上'} and hour < 12:
        hour += 12
    elif period == '中午' and 1 <= hour < 3:
        hour += 12
    elif period in {'凌晨', '早上', '早晨', '上午'} and hour > 11:
        return None
    due = (reference + timedelta(days={'今天': 0, '今晚': 0, '明天': 1, '明早': 1, '后天': 2}[day])).replace(
        hour=int(hour), minute=int(minute), second=0, microsecond=0)
    # 夏令时不存在或重复的本地时刻不能静默选择其中一个。
    if due.astimezone(timezone.utc).astimezone(ZoneInfo(zone)) != due or due.replace(fold=0).utcoffset() != due.replace(fold=1).utcoffset():
        return None
    return due.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def planned_reminder_requirement(message):
    """只识别当前请求的明确提醒意图，称呼里的“叫我”不是定时动作。"""
    if not isinstance(message, str):
        return None
    message = message.strip()
    if re.search(r'^(?:你|请问|我想知道|我记得|帮我|请帮我|请)?(?:为什么|怎么|如何|有没有|是否|还记得|刚才|之前|上次|取消|删除|停止|停用|不用|不要)', message):
        return None
    timed = bool(re.search(
        r'今天|明天|后天|今晚|明早|早上|上午|中午|下午|傍晚|晚上|凌晨|'
        r'(?:周|星期|礼拜)[一二三四五六日天1-7]|'
        r'(?:\d{1,2}[:：]\d{2})|'
        r'(?:\d+|[一二三四五六七八九十百两半]+)\s*(?:点|时|分钟|小时|秒钟|秒)|'
        r'每天|每日|每晚|每早|每周|每月|定期|每隔', message))
    reminder = bool(re.search(r'提醒我|(?:创建|设置|添加|设个|设一|定个).{0,8}(?:提醒|闹钟)', message))
    # “十分钟后叫我”是提醒，“以后叫我小王”仍是称呼偏好。
    reminder |= timed and bool(re.search(r'叫醒我|叫我|喊我|通知我', message))
    if not reminder:
        return None
    if re.search(r'每天|每日|每晚|每早|每周|每月|定期|每隔', message):
        return 'recurring'
    return 'scheduled' if timed else 'untimed'


def validate_plan(message, proposals):
    """在派发前拒绝遗漏通知时间的提案；不自行猜时间或替用户增加提醒。"""
    requirement = planned_reminder_requirement(message)
    if requirement not in {'scheduled', 'recurring'}:
        return
    # 多项要求可能同时包含周期和一次提醒，不能把整句话的时间强加给每一项。
    if re.search(r'分别|顺便|另外|并且|然后|同时|以及|[，,；;。]\s*(?:再|还|并)', message):
        return
    for capability, arguments in proposals:
        if capability != 'reminder.create@v1':
            continue
        if reminder_subject_missing(message) and reminder_subject_missing(str(arguments.get('title', ''))):
            raise ReminderSubjectMissing()
        if requirement == 'recurring':
            raise HTTPException(422, '用户要求周期提醒，请使用 create_automation；一次提醒不能替代周期规则')
        if not arguments.get('due_at') or arguments.get('notify_at_due') is not True:
            raise HTTPException(422, '用户要求到时提醒，请使用 schedule_reminder 并提供带时区的 due_at 和 notify_at_due=true；时间不明确时先询问，不能只保存无通知的待办')


def reminder_subject_missing(value):
    """识别只重复时间的空事项；上下文中已有具体事项的有效标题仍可通过。"""
    remaining = re.sub(
        r'今天|明天|后天|今晚|明早|早上|上午|中午|下午|傍晚|晚上|凌晨|'
        r'(?:周|星期|礼拜)[一二三四五六日天1-7]|'
        r'[\d一二三四五六七八九十百两半]+|分钟|小时|秒钟|[年月日点时分秒:：]|'
        r'请|麻烦|帮|给|到|在|后|之前|提醒|定时|事项|一下|一声|我|你|吧|哦|呀|啊|呗', '', value)
    return not remaining.strip(' \t\n，,。.!！?？~～')


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
