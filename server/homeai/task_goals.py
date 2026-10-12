"""可确定的单一动作边界，不能由模型把一次用户请求扩成重复副作用。"""
import re


def single_reminder(message):
    from .reminders import planned_reminder_requirement
    requirement=planned_reminder_requirement(message)
    if requirement not in {'untimed','scheduled'}:return False
    if re.search(r'两个|两条|多个|多条|分别|另外|另一个|另一条|各|[2-9]\s*(?:个|条)|再.{0,8}(?:提醒|创建)|(?:搜索|联网|查一下|读取|发送|分享|生成|打开|运行|通知.*(?:成员|家人))',message):return False
    # 仅对可确定的单一动作收束工具。复合请求不能因第一条提醒成功就被截断。
    if re.search(r'顺便|并且|然后|以及|同时|此外|接着|再帮我|也帮我|还(?:要|请|帮|需要)|[，,；;。\n]\s*(?:再|并|还)|总结|分析|统计|对比|比较|计算',message):return False
    if len(re.findall(r'(?:\d+|[一二三四五六七八九十两半]+)(?:点|[:：]\d{2}|分钟后|小时后)',message))>1:return False
    return True


def reminder_satisfied(body,completed):
    return bool(single_reminder(body.get('message','')) and body.get('steps')
        and all(step['capability']=='reminder.create@v1' for step in body['steps'])
        and all(index in completed and completed[index].status=='SUCCEEDED' for index in range(len(body['steps']))))


def supplied_image_analysis(message):
    """已把实际图像送入模型的纯分析，不要求凭空添加一次工具调用。"""
    import re
    if re.search(r'发|寄|删|建|操作|打开|运行|执行|提醒|自动化|搜索|联网|下载|导出|保存|上传|通知|购买|预订|更新|修改|写入|控制|下单', message):
        return False
    return bool(re.fullmatch(r'(?:请|请你|帮我|麻烦你)?(?:分析|描述|识别|总结|看一下|看看)(?:一下)?(?:这些|这个|这些上传的|上传的|这张|这份)?(?:附件|图片|照片|图像)(?:的内容|内容|是什么)?[。！!？?\s]*', message.strip()))
