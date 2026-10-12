"""读取真实模型增量；不把思考内容或未结束的工具规划当作用户回答。"""
import json
import re
from fastapi import HTTPException


def visible_text(raw):
    text=re.sub(r'<think>.*?</think>','',raw,flags=re.I|re.S)
    if '<think>' in text.lower():text=text[:text.lower().index('<think>')]
    text=re.sub(r'^\s*</think>\s*','',text,flags=re.I)
    for width in range(1,min(9,len(text))+1):
        suffix=text[-width:].lower()
        if any(tag.startswith(suffix) for tag in ('<think>','</think>')):
            text=text[:-width];break
    return text.strip()


async def generate(client,url,payload,headers,on_text):
    text='';finish=None;usage=None;done=False
    async with client.stream('POST',url,json={**payload,'stream':True,'stream_options':{'include_usage':True}},headers=headers) as response:
        response.raise_for_status()
        if 'text/event-stream' not in response.headers.get('content-type',''):
            # 有些兼容服务忽略 stream；保留真实完整结果，不伪造打字动画。
            data=await response.aread()
            if len(data)>2*1024*1024:raise HTTPException(502,'模型响应超过限制')
            return json.loads(data)
        async for line in response.aiter_lines():
            if len(line)>256*1024:raise HTTPException(502,'模型增量超过限制')
            if not line.startswith('data:'):continue
            data=line[5:].strip()
            if data=='[DONE]':done=True;break
            if not data:continue
            value=json.loads(data)
            if value.get('error'):raise HTTPException(502,'模型流式生成失败')
            if value.get('usage'):usage=value['usage']
            for choice in value.get('choices',[]):
                if choice.get('index',0)!=0:continue
                delta=choice.get('delta',{})
                if delta.get('tool_calls'):raise HTTPException(502,'普通回答流意外返回工具调用')
                part=delta.get('content')
                if part is not None and not isinstance(part,str):raise HTTPException(502,'不支持的模型增量格式')
                text+=part or ''
                if len(text)>200000:raise HTTPException(502,'模型回答超过限制')
                if part:await on_text(visible_text(text))
                if choice.get('finish_reason'):finish=choice['finish_reason']
    if not done or finish not in {'stop','length'}:raise HTTPException(502,'模型生成连接中断，回答尚未完成')
    result={'choices':[{'message':{'role':'assistant','content':text},'finish_reason':finish}]}
    if usage:result['usage']=usage
    return result
