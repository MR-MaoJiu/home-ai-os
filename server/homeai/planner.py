"""模型只提交结构化操作建议，执行风险由核心重新判定。"""
import json
from fastapi import HTTPException
from .policy import CAPABILITIES

TOOLS = [
    {"type":"function","function":{"name":"search_memories","description":"检索当前用户已授权的记忆","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"],"additionalProperties":False}}},
    {"type":"function","function":{"name":"search_calendar","description":"检索当前用户已授权的日程","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"],"additionalProperties":False}}},
    {"type":"function","function":{"name":"create_reminder","description":"每次调用只创建一条家庭服务器提醒。多个事项必须分别调用，不能合并标题。不表示已写入手机系统","parameters":{"type":"object","properties":{"title":{"type":"string"}},"required":["title"],"additionalProperties":False}}},
    {"type":"function","function":{"name":"read_home_states","description":"查询已接入 Home Assistant 的设备状态","parameters":{"type":"object","properties":{},"additionalProperties":False}}},
]
MAPPING={'search_memories':'memory.search@v1','search_calendar':'calendar.search@v1','create_reminder':'reminder.create@v1','read_home_states':'home.states@v1'}


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
    if any(not isinstance(v,str) or len(v)>2000 for v in args.values()):raise HTTPException(422,'工具参数值无效')
    return MAPPING[name],args


def decode_proposals(result, max_calls=16):
    calls = result.get('choices', [{}])[0].get('message', {}).get('tool_calls') or []
    if not isinstance(calls, list) or len(calls) > max_calls:
        raise HTTPException(422, '模型工具调用超过剩余步骤预算')
    return [decode_proposal({'choices': [{'message': {'tool_calls': [call]}}]}) for call in calls]
