"""聊天模型响应只使用最终正文或明确工具调用；诊断不记录思考和拒答正文。"""
import copy
from fastapi import HTTPException
from .source_fidelity import clean_answer

FINISH_REASONS = {'stop','length','tool_calls','function_call','content_filter','aborted','insufficient_system_resource'}
REASONS = {
    'empty_choices':'模型接口返回了空的 choices，未提供任何候选答复；请检查接口兼容性后重试。',
    'invalid_schema':'模型返回的聊天响应结构无效，未执行后续工具；请检查接口兼容性。',
    'unsupported_content':'模型返回了不支持的正文格式，未将其视为成功答复。',
    'output_limit':'模型已达到本次输出上限，未取得完整答复；请检查思考模式与单次输出限制后重试。已完成步骤不会重放。',
    'content_filter':'模型服务过滤了本次响应，没有返回可用答复；请调整请求后重新提交。',
    'refusal':'模型明确拒绝了本次请求，没有返回可用答复；请调整请求后重新提交。',
    'reasoning_only':'模型只返回了思考过程，没有最终答复；请检查模型思考模式与单次输出限制。',
    'empty_response':'模型返回了空正文，也没有工具调用；请重试或检查模型接口。',
    'upstream_interrupted':'模型生成被上游中断，未取得完整答复；已完成步骤保留，可稍后重试。',
    'unexpected_tool_calls':'本次只请求文字答复，模型却返回了操作指令；这些操作未执行。',
}


def diagnostics(response):
    choices = response.get('choices') if isinstance(response, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = choice.get('message')
    message = message if isinstance(message, dict) else {}
    content, reasoning = message.get('content'), message.get('reasoning_content')
    finish = choice.get('finish_reason')
    kind = type(content).__name__
    info = {'choice_count':len(choices) if isinstance(choices,list) else 0,
        'finish_reason':finish if finish is None or (isinstance(finish,str) and finish in FINISH_REASONS) else 'other',
        'content_type':kind if kind in {'str','list','dict','NoneType','bool','int','float'} else 'other',
        'content_characters':len(content) if isinstance(content,str) else 0,
        'reasoning_characters':len(reasoning) if isinstance(reasoning,str) else 0,
        'has_reasoning':bool(reasoning), 'has_refusal':bool(message.get('refusal')),
        'tool_call_count':len(message['tool_calls']) if isinstance(message.get('tool_calls'),list) else 0}
    usage = response.get('usage') if isinstance(response,dict) else None
    if isinstance(usage,dict):
        for key in ('prompt_tokens','completion_tokens','total_tokens'):
            if type(usage.get(key)) is int and usage[key]>=0: info[key]=usage[key]
        details=usage.get('completion_tokens_details')
        if isinstance(details,dict) and type(details.get('reasoning_tokens')) is int and details['reasoning_tokens']>=0:
            info['reasoning_tokens']=details['reasoning_tokens']
    return info


class ModelResponseError(HTTPException):
    def __init__(self, code, response):
        super().__init__(502, REASONS[code])
        self.code, self.diagnostics = code, diagnostics(response)


def assistant_message(response):
    def reject(code): raise ModelResponseError(code,response)
    if not isinstance(response,dict): reject('invalid_schema')
    choices=response.get('choices')
    if isinstance(choices,list) and not choices: reject('empty_choices')
    if not isinstance(choices,list) or not choices or not isinstance(choices[0],dict): reject('invalid_schema')
    choice=choices[0];message=choice.get('message')
    if not isinstance(message,dict) or (message.get('role') is not None and message.get('role')!='assistant'): reject('invalid_schema')
    finish=choice.get('finish_reason')
    if finish is not None and not isinstance(finish,str): reject('invalid_schema')
    if message.get('refusal'): reject('refusal')
    if finish=='content_filter': reject('content_filter')
    if finish=='length': reject('output_limit')
    if finish in {'aborted','insufficient_system_resource'}: reject('upstream_interrupted')
    if finish is not None and finish not in FINISH_REASONS: reject('invalid_schema')
    calls=message.get('tool_calls')
    if calls is not None and (not isinstance(calls,list) or any(not isinstance(call,dict) for call in calls)):
        reject('invalid_schema')
    content=message.get('content')
    if isinstance(content,list):
        parts=[]
        if len(content)>256: reject('unsupported_content')
        for part in content:
            if not isinstance(part,dict): reject('unsupported_content')
            if part.get('type')=='refusal': reject('refusal')
            if not isinstance(part.get('type'),str) or part['type'] not in {'text','output_text'} or not isinstance(part.get('text'),str): reject('unsupported_content')
            parts.append(part['text'])
        content=''.join(parts)
    elif content is not None and not isinstance(content,str): reject('unsupported_content')
    text=clean_answer(content or '')
    if not text and not calls:
        reject('reasoning_only' if message.get('reasoning_content') or (content and '<think>' in content.lower()) else 'empty_response')
    normalized={key:copy.deepcopy(value) for key,value in message.items() if key not in {'reasoning_content','reasoning','thinking','refusal'}}
    normalized.update(role='assistant',content=text)
    return normalized


def normalize_response(response, *, allow_tools=True):
    message=assistant_message(response)
    if message.get('tool_calls') and not allow_tools: raise ModelResponseError('unexpected_tool_calls',response)
    result={key:copy.deepcopy(value) for key,value in response.items() if key!='choices'}
    result['choices']=[{**response['choices'][0],'message':message}]
    return result


def record_diagnostics(body,response,call_key,provider_id,max_tokens):
    info=diagnostics(response)
    try:
        assistant_message(response);info['code']='valid_response'
    except ModelResponseError as error:
        info['code']=error.code
    except Exception:
        # 诊断记录不能阻止已经确认的费用结算；执行前仍会严格验证同一响应。
        info['code']='invalid_schema'
    info.update(call_key=call_key,provider_id=provider_id,max_output_tokens=max_tokens)
    previous=[item for item in body.get('_model_response_diagnostics',[]) if item.get('call_key')!=call_key]
    body['_model_response_diagnostics']=[*previous,info][-20:]
