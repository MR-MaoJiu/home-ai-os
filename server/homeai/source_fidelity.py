"""清理模型协议残留，并让短标识符摘抄保持已授权原文的大小写。"""
import re


def clean_answer(value):
    return re.sub(r'^(?:<think>.*?</think>|</think>)\s*','',value.strip(),flags=re.S).strip()


def faithful_answer(app,db,actor,body,value):
    answer=clean_answer(value)
    # 只处理明确摘抄文件开头的请求，不校正推断、自然语言答案或任意相似文本。
    request=body.get('message','')
    if re.search(r'转换|改成|改为|转成|小写|大写|翻译|重写|改写',request):return answer
    if not re.search(r'最前面|首行|第一行|开头',request) or not re.search(r'只回复|只返回|只输出|原样|原文|逐字|读取.*(?:标记|代号|编号)',request):
        return answer
    from .client_actions import read_authorized_record
    sources=set(body.get('record_ids',[]))|set(body.get('_historical_record_ids',[]))
    candidates={}
    for identifier,link in body.get('_derived_task_sources',{}).items():
        if link['source_id'] not in sources:continue
        record=read_authorized_record(db,actor,identifier,body)
        payload=app.vault.open(record.payload,record.owner_id+':record:'+record.id)
        first=payload.get('markdown','').split('\n',1)[0].rstrip('\r')
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{1,199}',first):
            candidates.setdefault(first.casefold(),set()).add(first)
    for values in candidates.values():
        if len(values)!=1:continue
        original=next(iter(values))
        answer=re.sub(r'(?<![A-Za-z0-9_.:/-])'+re.escape(original)+r'(?![A-Za-z0-9_.:/-])',lambda _:original,answer,flags=re.I)
    return answer
