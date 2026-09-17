"""工具结果与答案缓存不延长来源数据授权。"""
from fastapi import HTTPException
from .data import read_record


def check_dependencies(db,actor,payload):
    versions=payload.get('_record_dependencies',{})
    for identifier in set(payload.get('record_ids',[]))|set(versions):
        record=read_record(db,actor,identifier)
        if record.sensitivity=='SECRET':raise HTTPException(403,'来源已变为秘密，结果不可继续使用')
        if identifier in versions and record.version!=versions[identifier]:raise HTTPException(409,'来源版本已变化，请重新执行')


def capture_result_dependencies(db,actor,payload,capability,result):
    versions=payload.setdefault('_record_dependencies',{})
    if capability=='knowledge.search@v1':
        entries=[{'id':row['record_id'],'version':row['version']} for row in result.get('matches',[])]
    elif capability in {'memory.search@v1','calendar.search@v1','memory.semantic.search@v1','memory.graph.search@v1'}:
        entries=result.get('records',[]) if isinstance(result,dict) else result
    elif capability=='document.parse@v1':
        entries=[]
        for identifier in (result.get('source_id'),result.get('record_id')):
            if identifier:
                record=read_record(db,actor,identifier);entries.append({'id':record.id,'version':record.version})
    else:entries=[]
    for entry in entries:
        if isinstance(entry,dict) and 'id' in entry and 'version' in entry:versions[entry['id']]=entry['version']
    if len(versions)>100:raise HTTPException(413,'结果来源数量超过限制')
    check_dependencies(db,actor,payload)
