"""模型只提交结构化操作建议，执行风险由核心重新判定。"""
import json
from fastapi import HTTPException
from .policy import CAPABILITIES

TOOLS = [
    {"type":"function","function":{"name":"list_automations","description":"查询个人及家庭自动化的ID、名称与状态，停用前先查询以确认目标。","parameters":{"type":"object","properties":{},"additionalProperties":False}}},
    {"type":"function","function":{"name":"stop_automation","description":"根据用户明确要求停用一条属于本人的自动化。","parameters":{"type":"object","properties":{"automation_id":{"type":"string"}},"required":["automation_id"],"additionalProperties":False}}},

    {"type":"function","function":{"name":"create_automation","description":"用户要求周期执行任务时，在服务端创建定时自动化。用户明确要求全家展示时 visibility=family，否则personal。家庭任务只读取家庭数据。","parameters":{"type":"object","properties":{"name":{"type":"string"},"cron":{"type":"string","description":"五段Cron：分 时 日 月 周"},"instruction":{"type":"string","description":"每次执行的任务要求"},"visibility":{"type":"string"}},"required":["name","cron","instruction","visibility"],"additionalProperties":False}}},
    {"type":"function","function":{"name":"remember_chat","description":"用户明确要求记住聊天内容时提出候选记忆，待用户在记忆页确认。不得复制文件或健康位置原始数据。","parameters":{"type":"object","properties":{"content":{"type":"string"}},"required":["content"],"additionalProperties":False}}},

    {"type":"function","function":{"name":"search_web","description":"搜索互联网公开资料，仅在用户要求联网信息时使用。查询词会发送给外部搜索引擎，公开查询由服务端自动执行；不得包含私人资料、健康信息或秘密。","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"],"additionalProperties":False}}},
    {"type":"function","function":{"name":"search_documents","description":"检索已解析且当前已授权的文档，回答文件内容问题前使用此工具，无匹配时说明未找到，不编造文件内容","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"],"additionalProperties":False}}},
    {"type":"function","function":{"name":"search_memories","description":"检索当前用户已授权的记忆","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"],"additionalProperties":False}}},
    {"type":"function","function":{"name":"search_calendar","description":"检索当前用户已授权的日程","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"],"additionalProperties":False}}},
    {"type":"function","function":{"name":"create_reminder","description":"创建一条没有指定时间的家庭提醒。多个事项分别调用；不要添加时间或通知参数。","parameters":{"type":"object","properties":{"title":{"type":"string"}},"required":["title"],"additionalProperties":False}}},
    {"type":"function","function":{"name":"schedule_reminder","description":"仅用户明确要求时间时创建定时家庭提醒。使用带时区的 ISO 8601 时间；不代表手机已经通知。","parameters":{"type":"object","properties":{"title":{"type":"string"},"due_at":{"type":"string","description":"带时区的 ISO 8601 绝对时间"},"notify_at_due":{"type":"boolean","description":"是否请求到期时通知"}},"required":["title","due_at","notify_at_due"],"additionalProperties":False}}},
    {"type":"function","function":{"name":"read_home_states","description":"查询已接入 Home Assistant 的设备状态","parameters":{"type":"object","properties":{},"additionalProperties":False}}},
]
MAPPING={'list_automations':'automation.list@v1','stop_automation':'automation.stop@v1','create_automation':'automation.create@v1','remember_chat':'memory.commit@v1','search_web':'web.search@v1','search_documents':'knowledge.search@v1','search_memories':'memory.search@v1','search_calendar':'calendar.search@v1','create_reminder':'reminder.create@v1','schedule_reminder':'reminder.create@v1','read_home_states':'home.states@v1'}


def decode_proposal(result):
    message=result.get('choices',[{}])[0].get('message',{})
    calls=message.get('tool_calls') or []
    if not calls:return None
    if len(calls)!=1:raise HTTPException(422,'本轮只允许一个工具操作，请拆分请求')
    function=calls[0].get('function',{})
    name=function.get('name')
    if name not in MAPPING:raise HTTPException(403,'模型提出未授权工具')
    try:args=json.loads(function['arguments'])
    except Exception:raise HTTPException(422,'模型工具参数无效') from None
    schema=next(t['function']['parameters'] for t in TOOLS if t['function']['name']==name)
    if not isinstance(args,dict) or set(args)-set(schema['properties']) or set(schema.get('required',[]))-set(args):raise HTTPException(422,'工具参数不符合契约')
    for key,value in args.items():
        expected=schema['properties'][key]['type']
        if expected=='string' and (not isinstance(value,str) or len(value)>2000):raise HTTPException(422,'工具文本参数无效')
        if expected=='boolean' and type(value) is not bool:raise HTTPException(422,'工具布尔参数无效')
    if name in {'create_reminder','schedule_reminder'}:
        from .reminders import normalize
        args=normalize(args)
    return MAPPING[name],args


def decode_proposals(result, max_calls=16):
    calls = result.get('choices', [{}])[0].get('message', {}).get('tool_calls') or []
    if not isinstance(calls, list) or len(calls) > max_calls:
        raise HTTPException(422, '模型工具调用超过剩余步骤预算')
    return [decode_proposal({'choices': [{'message': {'tool_calls': [call]}}]}) for call in calls]
