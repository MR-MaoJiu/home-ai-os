"""照片分析只读取授权规范记录，拒绝 URL、秘密与客户端消息拼装。"""
import base64
from fastapi import HTTPException
from .data import read_record, serialize
from .privacy import ensure_model_safe


def prepare(db,actor,arguments,app):
    if not isinstance(arguments,dict) or set(arguments)-{'record_id','question'} or not isinstance(arguments.get('record_id'),str):
        raise HTTPException(422,'照片分析必须引用已同步照片的 record_id')
    question=arguments.get('question','请用中文描述这张图片的可见内容，不猜测人物身份。')
    if not isinstance(question,str) or not 1<=len(question.strip())<=1000:
        raise HTTPException(422,'照片问题长度无效')
    ensure_model_safe(question)
    source=read_record(db,actor,arguments['record_id'])
    if source.kind!='photo.selected' or source.sensitivity=='SECRET':
        raise HTTPException(403,'只允许分析已授权的非秘密照片')
    content=serialize(source,app.vault)['payload'].get('content_base64')
    if not isinstance(content,str) or len(content)>14_000_000:
        raise HTTPException(413,'照片内容缺失或过大')
    try:decoded=base64.b64decode(content,validate=True)
    except Exception:raise HTTPException(422,'照片编码无效') from None
    if not 1<=len(decoded)<=10*1024*1024:
        raise HTTPException(413,'照片超过 10 MB')
    return source,{'content_base64':content,'question':question}


def result(db,actor,source_id,version,value):
    source=read_record(db,actor,source_id)
    if source.version!=version or source.sensitivity=='SECRET' or source.kind!='photo.selected':
        raise HTTPException(409,'分析期间照片发生变化，请重新提交')
    if not isinstance(value,dict) or not isinstance(value.get('text'),str) or not 1<=len(value['text'].strip())<=20000:
        raise HTTPException(502,'视觉模型没有返回有效分析')
    return {'text':value['text'],'source_id':source.id,'source_version':version,
            'status':'analyzed','model_output':True}
