"""资源工具只返回已入库附件；既有资料、公开获取和生成图片明确区分。"""
import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit, unquote
from fastapi import HTTPException
from sqlalchemy import select
from .db import Invocation, now
from .media import limits_for


def tool(name, description, properties, required):
    return {'type':'function', 'function':{'name':name, 'description':description,
        'parameters':{'type':'object', 'properties':properties, 'required':required, 'additionalProperties':False}}}


TOOLS = [
    tool('send_resource', '把已获准的现有图片、视频或文件作为聊天附件交给用户。先检索资料取得真实record_id，不能伪造ID或将检索摘要当文件。',
         {'record_id':{'type':'string'}}, ['record_id']),
    tool('fetch_resource', '下载用户明确给出或本任务真实搜索得到的公开HTTPS资源，并发回附件。找原图/资料/视频时用此能力，不得用生图代替真实资料；不携带账号、Cookie或私人内容。',
         {'url':{'type':'string'}, 'kind':{'type':'string','enum':['image','video','file']}}, ['url','kind']),
    tool('collect_resources', '用受限资源脚本从真实公开页面提取并下载资源。仅支持赋值、字符串、列表和三个函数；示例：page = fetch_resource("https://来源页面")\nlinks = extract_links(page, kind="image")\nresult = select_resources(links, limit=1)。kind可为image/video/file。初始URL必须来自用户当前输入或真实搜索结果。禁止import、循环、属性、文件系统和任意代码执行。最多3个附件，不能把生成图片伪称抓取原图。',
         {'script':{'type':'string'}, 'kind':{'type':'string','enum':['image','video','file']}}, ['script','kind']),
    tool('generate_image', '仅用户要求创作/生成一张新图片时使用专用生图模型，不能作为找原图或检索照片的替代。返回标明AI生成的图片附件；默认1024x1024。',
         {'prompt':{'type':'string'}, 'size':{'type':'string','enum':['1024x1024','1536x1024','1024x1536','auto']}}, ['prompt']),
    tool('edit_image', '仅用户明确要求修改已上传的本人图片时使用；record_id必须为真实原图，不能凭空生成原图。云端编辑须对像素披露单独授权。',
         {'prompt':{'type':'string'}, 'record_id':{'type':'string'}, 'size':{'type':'string','enum':['1024x1024','1536x1024','1024x1536','auto']}}, ['prompt','record_id']),
]
MAPPING = {'send_resource':'resource.send@v1', 'fetch_resource':'resource.fetch@v1',
           'collect_resources':'resource.script@v1', 'generate_image':'image.generate@v1', 'edit_image':'image.edit@v1'}


def allowed_urls(app, db, actor, task, body):
    # 来源白名单不从模型生成的理由、任意消息或工具参数中推导。
    urls = {value.rstrip('。？，；！、,.!;') for value in re.findall(r'https://[^\s<>"“”‘’\]\)]+', body.get('message', ''))}
    for row in db.scalars(select(Invocation).where(Invocation.task_id == task.id,
            Invocation.owner_id == actor.user_id, Invocation.capability == 'web.search@v1', Invocation.status == 'SUCCEEDED')):
        if row.result:
            result = app.vault.open(row.result, actor.user_id + ':invocation-result:' + row.id)
            urls.update(item['url'] for item in result.get('results', []) if isinstance(item, dict) and isinstance(item.get('url'), str))
    return urls


async def execute(app, db, actor, task, body, capability, args, invocation_id):
    from .resource_assets import store_asset, send_existing
    if capability == 'resource.send@v1': return send_existing(app, db, actor, body, args)
    field = 'script' if capability == 'resource.script@v1' else 'url'
    if set(args) != {field, 'kind'} or not isinstance(args.get(field), str) or args.get('kind') not in {'image','video','file'}:
        raise HTTPException(422, '资源请求不符合契约')
    from .privacy import ensure_model_safe
    ensure_model_safe(args[field])
    from .resource_fetch import fetch_public, execute_resource_script
    maximum = limits_for(app)[args['kind']]
    permitted = allowed_urls(app, db, actor, task, body)
    if capability == 'resource.script@v1':
        resources = await execute_resource_script(args['script'], allowed_urls=permitted,
            max_resources=3, max_bytes=maximum, total_bytes=min(200*1024*1024, maximum*3))
    else:
        resources = [await fetch_public(args['url'], max_bytes=maximum, allowed_urls=permitted)]
    from .result_access import check_dependencies
    check_dependencies(db, actor, body)
    db.refresh(task)
    if task.cancel_requested or task.deadline <= now(): raise HTTPException(409, '任务已取消或过期，资源未发布')
    parts = []
    for index, (raw, metadata) in enumerate(resources):
        url = metadata.get('final_url') or metadata.get('url') or ''
        name = unquote(PurePosixPath(urlsplit(url).path).name)[:160]
        name = re.sub(r'[\\/\x00-\x1f]', '_', name) or '资料'
        if args['kind'] == 'image':
            from .media_formats import image_type
            extension = {'image/png':'png','image/jpeg':'jpg','image/webp':'webp','image/gif':'gif'}.get(image_type(raw))
            if not extension: raise HTTPException(422, '来源没有返回图片，不能把网页或生成内容冒充原图')
            if not name.lower().endswith(('.png','.jpg','.jpeg','.webp','.gif')): name += '.' + extension
        provenance = {'origin':'script_retrieved' if capability == 'resource.script@v1' else 'retrieved',
                      'source_url':url, 'retrieved_at':now()}
        result = store_asset(app, db, actor, task, invocation_id + ':' + str(index), raw, name, args['kind'], provenance)
        parts.extend(result['parts'])
    if not parts: raise HTTPException(404, '页面没有找到符合条件的可下载资源；未生成替代内容')
    return {'status':'available', 'text':'已取得公开来源的资源。', 'parts':parts}
