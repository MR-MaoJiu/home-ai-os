"""搜索覆盖度与调用成功分开记录，部分引擎故障不抹掉已有证据。"""
from urllib.parse import urlparse


def unavailable(reason, failures=None):
    return {'results':[], 'unresponsive_engines':failures or [], 'status':'unavailable',
            'error_reason':reason, 'warning':'本轮没有取得可用结果，搜索服务或部分上游引擎未能正常响应。',
            'content_trust':'untrusted_web'}


def normalize(value):
    if not isinstance(value,dict) or not isinstance(value.get('results'),list):
        return unavailable('invalid_response')
    entries=[];seen=set();discarded=0
    # 按有效条目预算截断，不因最前面的无效条目丢掉后面的真实来源。
    for item in value['results'][:200]:
        if not isinstance(item,dict):discarded+=1;continue
        url=item.get('url')
        try:parsed=urlparse(url) if isinstance(url,str) else None
        except ValueError:parsed=None
        if not parsed or parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username is not None or parsed.password is not None or len(url)>4000:
            discarded+=1;continue
        if url in seen:continue
        seen.add(url)
        entries.append({'title':str(item.get('title',''))[:500],'url':url,
                        'content':str(item.get('content',''))[:2000],'engine':str(item.get('engine',''))[:100]})
        if len(entries)>=10:break
    failed=value.get('unresponsive_engines',[])
    failures=[{'engine':str(item[0])[:100],'reason':str(item[1])[:200]} for item in failed[:30] if isinstance(item,(list,tuple)) and len(item)>=2] if isinstance(failed,list) else []
    if not entries and (failures or discarded):
        return unavailable('upstream_incomplete' if failures else 'no_safe_results',failures)
    result={'results':entries,'unresponsive_engines':failures,'status':'partial' if failures else 'ok' if entries else 'empty','content_trust':'untrusted_web'}
    if failures:result['warning']='部分搜索引擎未响应，当前结果仍可使用，但覆盖范围可能不完整。'
    return result


def warnings(results):
    notices=[]
    if any(item.get('status')=='unavailable' for item in results):
        notices.append('部分补充搜索未完成，以下结论仅基于已取得的有效来源。' if any(item.get('results') for item in results) else '本次联网搜索未取得可核实的来源，暂时无法根据搜索结果回答。')
    elif any(item.get('status')=='partial' for item in results):
        notices.append('部分搜索引擎未响应，结果覆盖范围可能不完整。')
    return notices
