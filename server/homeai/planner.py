"""模型只提交结构化操作建议，执行风险由核心重新判定。"""
import json
from fastapi import HTTPException
from .policy import CAPABILITIES

TOOLS = [
    {"type":"function","function":{"name":"list_automations","description":"查询个人及家庭自动化的ID、名称与状态，停用前先查询以确认目标。","parameters":{"type":"object","properties":{},"additionalProperties":False}}},
    {"type":"function","function":{"name":"stop_automation","description":"根据用户明确要求停用一条属于本人的自动化。","parameters":{"type":"object","properties":{"automation_id":{"type":"string"}},"required":["automation_id"],"additionalProperties":False}}},

    {"type":"function","function":{"name":"create_automation","description":"用户要求周期执行任务时，在服务端创建定时自动化。用户明确要求全家展示时 visibility=family，否则personal。家庭任务只读取家庭数据。","parameters":{"type":"object","properties":{"name":{"type":"string"},"cron":{"type":"string","description":"五段Cron：分 时 日 月 周"},"instruction":{"type":"string","description":"每次执行的任务要求"},"visibility":{"type":"string"}},"required":["name","cron","instruction","visibility"],"additionalProperties":False}}},
    {"type":"function","function":{"name":"remember_chat","description":"用户明确要求记住本人聊天内容时直接保存记忆，不需再次点击确认。不得复制文件或健康位置原始数据。","parameters":{"type":"object","properties":{"content":{"type":"string"}},"required":["content"],"additionalProperties":False}}},

    {"type":"function","function":{"name":"search_web","description":"搜索互联网公开资料，仅在用户要求联网信息时使用。查询词会发送给外部搜索引擎，公开查询由服务端自动执行；不得包含私人资料、健康信息或秘密。","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"],"additionalProperties":False}}},
    {"type":"function","function":{"name":"search_documents","description":"已提供的附件节选不足以回答时，检索当前授权文档的其他段落。节选已经包含答案时直接使用原文，不要重复查询相同关键词。无匹配不代表节选不存在。","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"],"additionalProperties":False}}},
    {"type":"function","function":{"name":"search_memories","description":"检索当前用户已授权的记忆","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"],"additionalProperties":False}}},
    {"type":"function","function":{"name":"search_calendar","description":"检索当前用户已授权的日程","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"],"additionalProperties":False}}},
    {"type":"function","function":{"name":"create_reminder","description":"创建一条没有指定时间的家庭提醒。多个事项分别调用；不要添加时间或通知参数。","parameters":{"type":"object","properties":{"title":{"type":"string"}},"required":["title"],"additionalProperties":False}}},
    {"type":"function","function":{"name":"schedule_reminder","description":"仅用户明确要求时间时创建定时家庭提醒。使用带时区的 ISO 8601 时间；不代表手机已经通知。","parameters":{"type":"object","properties":{"title":{"type":"string"},"due_at":{"type":"string","description":"带时区的 ISO 8601 绝对时间"},"notify_at_due":{"type":"boolean","description":"是否请求到期时通知"}},"required":["title","due_at","notify_at_due"],"additionalProperties":False}}},
    {"type":"function","function":{"name":"read_home_states","description":"只读取已接入 Home Assistant 的灯、开关、温控器等智能家居实体状态。不用于查询家庭成员、iPhone、Home AI服务器或联网搜索。","parameters":{"type":"object","properties":{},"additionalProperties":False}}},
]
MAPPING={'list_automations':'automation.list@v1','stop_automation':'automation.stop@v1','create_automation':'automation.create@v1','remember_chat':'memory.commit@v1','search_web':'web.search@v1','search_documents':'knowledge.search@v1','search_memories':'memory.search@v1','search_calendar':'calendar.search@v1','create_reminder':'reminder.create@v1','schedule_reminder':'reminder.create@v1','read_home_states':'home.states@v1'}
from .client_actions import CLIENT_TOOLS, CLIENT_MAPPING
TOOLS.extend(CLIENT_TOOLS)
MAPPING.update(CLIENT_MAPPING)
from .voice_calls import TOOL as VOICE_CALL_TOOL
TOOLS.append(VOICE_CALL_TOOL)
MAPPING['call_user'] = 'voice.call@v1'
from .presentations import TOOL as PRESENTATION_TOOL
TOOLS.append(PRESENTATION_TOOL)
MAPPING['render_table']='presentation.render@v1'
from .resource_tools import TOOLS as RESOURCE_TOOLS, MAPPING as RESOURCE_MAPPING
TOOLS.extend(RESOURCE_TOOLS)
MAPPING.update(RESOURCE_MAPPING)
from .shell_tools import TOOLS as SHELL_TOOLS, MAPPING as SHELL_MAPPING
TOOLS.extend(SHELL_TOOLS)
MAPPING.update(SHELL_MAPPING)
from .host_inspection_tools import TOOL as HOST_INSPECTION_TOOL
TOOLS.append(HOST_INSPECTION_TOOL)
MAPPING['inspect_server']='host.inspect@v1'
from .android_tools import TOOLS as ANDROID_TOOLS, MAPPING as ANDROID_MAPPING
TOOLS.extend(ANDROID_TOOLS)
MAPPING.update(ANDROID_MAPPING)


def personal_health_types(message):
    """只为明确的本人健康整理发现账本，不把健康科普变成私人数据读取。"""
    import re
    topics = r'健康|睡眠|心率|步数|体重|血氧|呼吸频率|体温|体脂|活动能量|运动时间|运动距离|静息能量'
    if re.search(r'科普|原理|是什么|什么意思|什么叫', message) and not re.search(r'(?:我的|本人|个人).{0,12}(?:数据|记录|报告)', message):
        return None
    if re.search(r'他(?:人|们|的)|她(?:们|的)|家人|成员|爸爸|妈妈|父亲|母亲|孩子|妻子|丈夫', message):
        return None
    personal = re.search(r'(?:我的|本人(?:的)?|个人(?:的)?|自己的).{0,14}(?:' + topics + r')', message)
    report = re.search(r'(?:整理|分析|生成|做|出).{0,16}(?:健康|睡眠)(?:数据|报告)', message)
    if not re.search(topics, message) or not (personal or report):
        return None
    return mentioned_health_types(message)


def mentioned_health_types(message):
    """只解析当前问题明确提到的指标，不决定授权或自动扩大指标范围。"""
    import re
    mappings = {'sleep':r'睡眠', 'steps':r'步数', 'body_mass':r'体重',
        'resting_heart_rate':r'静息心率', 'heart_rate_variability':r'心率变异|HRV',
        'respiratory_rate':r'呼吸频率', 'oxygen_saturation':r'血氧',
        'active_energy':r'活动能量', 'basal_energy':r'静息能量|基础代谢',
        'exercise_minutes':r'运动时间', 'walking_running_distance':r'运动距离|步行距离|跑步距离',
        'body_temperature':r'体温', 'body_fat_percentage':r'体脂'}
    found = [kind for kind, pattern in mappings.items() if re.search(pattern, message, re.I)]
    if re.search('心率', re.sub(r'静息心率|心率变异(?:性)?', '', message)):
        found.append('heart_rate')
    return found


def requires_live_tools(message):
    """明确采集请求不能被小模型误分为闲聊后关闭工具。"""
    import re
    health=re.search(r'健康|心率|睡眠|步数|体重|HealthKit',message,re.I)
    request=re.search(r'我的|查询|查看|读取|获取|采集|分析|授权|权限|最近|今天|昨天',message)
    server=re.search(r'服务器|主机|电脑|Mac',message,re.I) and re.search(r'CPU|内存|截屏|截图|占用',message,re.I)
    command=re.search(r'Shell|终端|命令|脚本',message,re.I) and re.search(r'执行|运行|编辑|修改|删除|安装|卸载',message)
    android=re.search(r'安卓|Android|模拟器',message,re.I) and re.search(r'操作|打开|查看|运行|使用|接管|点击',message)
    app_task = re.search(r'点外卖|叫车|打车|网约车|订酒店', message) or (re.search(r'美团|饿了么|滴滴|支付宝|淘宝|京东|微信|高德|携程', message) and re.search(r'打开|操作|查看订单|下单|预订|点餐|发消息|查订单', message))
    explicit_search=re.search(r'^(?:请|请你|帮我|帮忙|麻烦你)?(?:联网)?(?:搜索|搜一下|搜一搜|查一下|查一查|查询)',message.strip()) and not re.search(r'怎么|如何|原理|是什么',message)
    from .reminders import planned_reminder_requirement
    reminder=planned_reminder_requirement(message) is not None or re.search(r'(?:取消|停用).{0,12}(?:提醒|自动化|日程)|(?:创建|设置|添加).{0,12}(?:自动化|日程)',message)
    explicit_memory=re.search(r'^(?:请)?(?:记住|记一下|记下来)',message.strip())
    current_weather=re.search(r'今天|明天|后天|本周|现在|实时|周末',message) and re.search(r'天气|气温|下雨',message)
    return bool(personal_health_types(message) is not None or (health and request) or server or command or android or app_task or explicit_search or reminder or explicit_memory or current_weather)


def decode_proposal(result, *, allowed_names=None):
    from .model_responses import assistant_message
    message=assistant_message(result)
    calls=message.get('tool_calls') or []
    if not calls:return None
    if len(calls)!=1:raise HTTPException(422,'本轮只允许一个工具操作，请拆分请求')
    function=calls[0].get('function',{})
    name=function.get('name')
    if name not in MAPPING:raise HTTPException(403,'模型提出未授权工具')
    if allowed_names is not None and name not in allowed_names:
        raise HTTPException(403,'模型提出了本轮未开放的能力，尚未配置或当前成员没有使用授权；未执行此操作')
    try:args=json.loads(function['arguments'])
    except Exception:raise HTTPException(422,'模型工具参数无效') from None
    schema=next(t['function']['parameters'] for t in TOOLS if t['function']['name']==name)
    if not isinstance(args,dict) or set(args)-set(schema['properties']) or set(schema.get('required',[]))-set(args):raise HTTPException(422,'工具参数不符合契约')
    for key,value in args.items():
        expected=schema['properties'][key]['type']
        if expected=='string' and (not isinstance(value,str) or len(value)>schema['properties'][key].get('maxLength',2000)):raise HTTPException(422,'工具文本参数无效')
        if expected=='integer' and type(value) is not int:raise HTTPException(422,'工具整数参数无效')
        if expected=='boolean' and type(value) is not bool:raise HTTPException(422,'工具布尔参数无效')
        if expected=='object' and (not isinstance(value,dict) or len(json.dumps(value,ensure_ascii=False))>10000):raise HTTPException(422,'工具对象参数无效')
        if expected=='array' and (not isinstance(value,list) or len(json.dumps(value,ensure_ascii=False))>50000):raise HTTPException(422,'工具列表参数无效')
    if name in {'create_reminder','schedule_reminder'}:
        from .reminders import normalize
        args=normalize(args)
    if name == 'ask_user':
        from .client_actions import clarification_arguments
        args = clarification_arguments(args)
    return MAPPING[name],args


def decode_proposals(result, max_calls=16, *, allowed_names=None):
    from .model_responses import assistant_message
    calls = assistant_message(result).get('tool_calls') or []
    if not isinstance(calls, list) or len(calls) > max_calls:
        raise HTTPException(422, '模型工具调用超过剩余步骤预算')
    return [decode_proposal({'choices': [{'message': {'tool_calls': [call]}}]}, allowed_names=allowed_names) for call in calls]
