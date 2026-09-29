"""模型上下文使用有界投影；规范记录及客户端详情仍保留完整内容。"""
import math
from .crypto import canonical
from .data import serialize


def model_record(record, vault, limit=4000):
    value=serialize(record,vault)
    payload=value['payload']
    # 任务补充可能包含否定或修正约束，不能默默截掉后半段；过大时由总上下文预算暂停。
    if record.kind=='client_input.text':return value
    if len(canonical(payload))<=limit:return value
    from .device_sync import display_metadata
    projected=display_metadata(payload)
    for key in ('content','text','markdown','description'):
        text=payload.get(key)
        if not isinstance(text,str):continue
        available=max(0,limit-len(canonical(projected))-200)
        projected[key+'_excerpt']=text.encode()[:available].decode('utf-8',errors='ignore')
    samples=payload.get('samples')
    if isinstance(samples,list):
        groups={}
        for sample in samples:
            if not isinstance(sample,dict):continue
            kind=str(sample.get('type',record.kind))[:80]
            group=groups.setdefault(kind,{'sample_count':0,'unit':str(sample.get('unit',''))[:30]})
            group['sample_count']+=1
            number=sample.get('value')
            if type(number) in (int,float) and math.isfinite(number) and kind!='sleep' and record.kind!='health.sleep':
                group['minimum_observed']=min(group.get('minimum_observed',number),number)
                group['maximum_observed']=max(group.get('maximum_observed',number),number)
        projected['sample_count']=len(samples)
        projected['sample_groups']=dict(list(groups.items())[:10])
        projected['note']='只给出原始样本数量与数值范围，不把重叠样本相加，不代表完整时序或医疗结论。'
    if len(canonical(projected))>limit:
        compact={}
        for key,item in projected.items():
            if len(canonical({**compact,key:item}))<=limit:compact[key]=item
        projected=compact
    preview={**value,'payload':projected,'payload_deferred':True,'context_note':'这里只是有界节选或元信息，不能声称已读取全文或每个样本；需要更多内容应检索或请求缩小范围。'}
    return preview
