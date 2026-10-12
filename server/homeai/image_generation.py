"""图片生成与编辑：独立用途、张数预留、隐私网关和规范附件回执。"""
import base64
import json
import re
from datetime import datetime, timezone
from fastapi import HTTPException
from sqlalchemy import String, Integer, select, func, text
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, Owned, now, uid
from .crypto import canonical, digest

IMAGE_CAPABILITIES = {'image.generate@v1', 'image.edit@v1'}


class ImageUsage(Owned, Base):
    __tablename__ = 'image_usage'
    task_id: Mapped[str] = mapped_column(String)
    call_key: Mapped[str] = mapped_column(String, unique=True)
    day: Mapped[str] = mapped_column(String)
    charged: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default='RESERVED')


def reserve_images(app, db, actor, task_id, call_key):
    from .model_routing import configuration, BudgetExceeded
    if db.bind.dialect.name == 'postgresql':
        key = int.from_bytes(__import__('hashlib').sha256(('images:' + actor.household_id).encode()).digest()[:8], 'big', signed=True)
        db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})
    config = configuration(app, db, actor.household_id)
    from .member_budgets import resolve_limits
    limits = resolve_limits(app, db, actor, config)
    if db.scalar(select(ImageUsage).where(ImageUsage.call_key == call_key)):
        raise HTTPException(409, '此图片调用已有费用预留，结果不明时不能重复生成')
    day = datetime.now(timezone.utc).date().isoformat()
    query = select(func.coalesce(func.sum(ImageUsage.charged), 0)).where(ImageUsage.household_id == actor.household_id, ImageUsage.day == day)
    for label, budget_scope, used, limit in (
        ('家庭','household_daily_images',db.scalar(query),config.get('household_daily_images',0)),
        ('本人','member_daily_images',db.scalar(query.where(ImageUsage.owner_id == actor.user_id)),limits['daily_images']),
    ):
        if not limit or used + 1 > limit:
            raise BudgetExceeded(f'{label}每日图片张数额度不足：已计入 {used}，本次需 1，上限 {limit}。请在管理端检查家庭或成员额度；图片费用不属于 Token 预算。',
                code=budget_scope+'_exceeded',scope=budget_scope,used=used,requested=1,limit=limit,recoverable=True)
    row = ImageUsage(id=uid(), owner_id=actor.user_id, household_id=actor.household_id,
                     task_id=task_id, call_key=call_key, day=day, charged=1)
    db.add(row); db.flush()
    return row


def validate_arguments(capability, args):
    allowed = {'prompt', 'size'} | ({'record_id'} if capability == 'image.edit@v1' else set())
    if set(args) - allowed or not isinstance(args.get('prompt'), str) or not 1 <= len(args['prompt'].strip()) <= 2000:
        raise HTTPException(422, '图片请求必须提供不超过 2000 字的描述')
    if args.get('size', '1024x1024') not in {'1024x1024', '1536x1024', '1024x1536', 'auto'}:
        raise HTTPException(422, '图片尺寸不受支持')
    if capability == 'image.edit@v1' and not isinstance(args.get('record_id'), str):
        raise HTTPException(422, '编辑图片必须指定已授权的原图记录')
    return args


async def image_bytes(response, max_bytes):
    entries = response.get('data') if isinstance(response, dict) else None
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        raise HTTPException(502, '图片模型未返回约定的一张图片')
    value = entries[0]
    if isinstance(value.get('b64_json'), str):
        if len(value['b64_json']) > (max_bytes + 2)//3*4:
            raise HTTPException(413, '图片模型返回超过附件大小限制')
        try: raw = base64.b64decode(value['b64_json'], validate=True)
        except ValueError: raise HTTPException(502, '图片模型返回了无效图像编码') from None
    elif isinstance(value.get('url'), str):
        from .resource_fetch import fetch_public
        raw, _ = await fetch_public(value['url'], max_bytes=max_bytes)
    else:
        raise HTTPException(502, '图片模型响应缺少图像内容')
    if not raw or len(raw) > max_bytes:
        raise HTTPException(413, '图片模型返回为空或超过大小限制')
    return raw


async def execute(app, db, actor, task, body, capability, args, invocation_id):
    from .model_routing import resolve_role
    from .resource_assets import store_asset
    from .media import limits_for, cloud_images
    validate_arguments(capability, args)
    if body.get('_agent'):
        instruction = body.get('message', '')
        operations = r'生成|生图|绘制|画|创作|设计|制作|generate|draw|create' if capability == 'image.generate@v1' else r'修改|编辑|修图|换|替换|去掉|移除|添加|加上|修复|抠图|扩图|调色|edit|modify|replace|remove'
        denied = re.search(r'(不要|禁止|不需要|不是).{0,8}(生成|生图|绘制|创作)|\b(do not|don.t)\s+(generate|draw|create)', instruction, re.I)
        if denied or not re.search(operations, instruction, re.I):
            raise HTTPException(422, '请先明确需要 AI 生成或编辑图片；查找已有照片和资料应使用检索获取，不能用生成内容替代')
    from .client_actions import read_authorized_record
    for identifier in set(body.get('record_ids', [])) | set(body.get('_record_dependencies', {})):
        if read_authorized_record(db, actor, identifier, body).owner_id != actor.user_id:
            raise HTTPException(403, '图片创作目前不能把其他成员的授权资料另存为本人图片，请由资料所有者发起创作')
    role = 'image_editing' if capability == 'image.edit@v1' else 'image_generation'
    manifest = resolve_role(app, db, actor, role)
    content = [{'type': 'text', 'text': args['prompt']}]
    source_versions = {}
    if capability == 'image.edit@v1':
        from .client_actions import read_authorized_record
        source = read_authorized_record(db, actor, args['record_id'], body)
        # 首版不把他人的临时授权原图永久派生为当前用户自己的文件。
        if source.owner_id != actor.user_id:
            raise HTTPException(403, '图片编辑目前仅支持本人的原图，请由资料所有者发起编辑')
        if source.kind != 'photo.file' or source.sensitivity == 'SECRET':
            raise HTTPException(422, '仅可编辑本人已上传的非秘密图片')
        source_versions[source.id] = source.version
        body.setdefault('_record_dependencies', {}).update(source_versions)
        content += await cloud_images(app, db, actor, source.id)
    # 图片用到的描述及原图都走已有网关；模型只收到获准的脱敏副本。
    arguments = {'messages': [{'role': 'user', 'content': content}], 'max_tokens': 1,
                 'image_options': {'size': args.get('size', '1024x1024')}}
    if manifest.cloud:
        from .cloud_gateway import invoke
        response = await invoke(app, db, actor, manifest, task, body, arguments, invocation_id, capability=capability)
    else:
        usage = reserve_images(app, db, actor, task.id, invocation_id)
        body['_image_dispatched'] = invocation_id
        task.request = app.vault.seal(body, actor.user_id + ':task:' + task.id)
        db.commit()
        try:
            response = await app.registry.invoke(db, actor, manifest, capability, arguments, invocation_id)
            usage.status = 'SUCCEEDED'
        except Exception:
            usage.status = 'UNKNOWN'; db.commit(); raise
    raw = await image_bytes(response, limits_for(app)['image'])
    from .result_access import check_dependencies
    check_dependencies(db, actor, body)
    db.refresh(task)
    if task.cancel_requested: raise HTTPException(409, '任务已取消，不再发布返回的图片')
    from .media_formats import image_type
    extension = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp', 'image/gif': 'gif'}.get(image_type(raw))
    if not extension: raise HTTPException(502, '图片模型返回的文件不是支持的图片格式')
    result = store_asset(app, db, actor, task, invocation_id, raw, ('编辑图片' if source_versions else '生成图片') + '.' + extension, 'image',
                        {'origin': 'edited' if source_versions else 'generated', 'provider': manifest.id,
                         'model': manifest.model, 'created_at': now(), 'source_versions': source_versions})
    mark_verified(db, actor, manifest, role)
    return result


def mark_verified(db, actor, manifest, role):
    from .model_routing import save_verification
    # 成员不能写管理员配置表；管理员实测由独立验证入口更新。
    if actor.role != 'infrastructure_owner': return
    save_verification(db, actor.household_id, manifest, {role: True, 'status': 'verified'}, {role})


async def probe(app, db, actor, manifest, *, generate=True, edit=False):
    """管理员明确确认费用后，用固定公开样例实测；不读取家庭资料。"""
    from .cloud_gateway import CloudPermit, image_sample
    from .model_routing import fingerprint
    from .media import probe as inspect_media, limits_for
    report = {'errors': {}}
    for enabled, capability, role in ((generate, 'image.generate@v1', 'image_generation'), (edit, 'image.edit@v1', 'image_editing')):
        if not enabled: continue
        report[role] = False
        if capability not in manifest.capabilities:
            report['errors'][role] = 'capability_not_mapped'; continue
        content = [{'type': 'text', 'text': 'Create a plain blue square on a white background.'}]
        if capability == 'image.edit@v1':
            sample = base64.b64decode(image_sample().split(',', 1)[1])
            inspected = inspect_media(sample, 'image', frames=True, cloud=True)
            content.append({'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + inspected['frames'][0]['content_base64']}})
        arguments = {'messages': [{'role': 'user', 'content': content}], 'image_options': {'size': '1024x1024'}, 'max_tokens': 1}
        key = 'image-probe:' + uid()
        usage = None
        try:
            await app.policy.check(actor, capability)
            usage = reserve_images(app, db, actor, key, key); db.commit()
            permit = CloudPermit(actor.user_id, fingerprint(manifest), digest(canonical(arguments)), now()+30, capability) if manifest.cloud else None
            response = await app.registry.invoke(db, actor, manifest, capability, arguments, key, cloud_permit=permit)
            raw = await image_bytes(response, limits_for(app)['image'])
            inspect_media(raw, 'image', max_bytes=limits_for(app)['image'])
            usage.status = 'SUCCEEDED'; report[role] = True
        except Exception as exc:
            if usage is not None: usage.status = 'UNKNOWN'
            report['errors'][role] = 'request_failed' if not isinstance(exc, HTTPException) else str(exc.detail)
        db.commit()
    report['status'] = 'verified' if any(report.get(role) for role in ('image_generation', 'image_editing')) else 'unavailable'
    return report


async def invoke_adapter(client, manifest, capability, arguments):
    """只接受已过网关的消息结构；图片编辑使用标准 multipart 文件字段。"""
    from .cloud_gateway import inspection_messages
    inspection_messages(arguments.get('messages'))
    prompt, images = [], []
    for message in arguments['messages']:
        content = message.get('content')
        if isinstance(content, str): prompt.append(content)
        elif isinstance(content, list):
            for part in content:
                if part['type'] == 'text': prompt.append(part['text'])
                else: images.append(base64.b64decode(part['image_url']['url'].split(',', 1)[1], validate=True))
    options = arguments.get('image_options', {})
    if set(options) - {'size'} or options.get('size', '1024x1024') not in {'1024x1024', '1536x1024', '1024x1536', 'auto'}:
        raise HTTPException(422, '图片生成参数无效')
    payload = {'model': manifest.model, 'prompt': '\n'.join(prompt), 'n': 1, 'size': options.get('size', '1024x1024')}
    if not payload['prompt'].strip() or len(payload['prompt']) > 10000:
        raise HTTPException(422, '图片描述为空或过长')
    if not (manifest.model or '').startswith('gpt-image'):
        payload['response_format'] = 'b64_json'
    if capability == 'image.edit@v1':
        if len(images) != 1: raise HTTPException(422, '每次编辑必须提供一张已校验的原图')
        kwargs = {'data': {key: str(value) for key, value in payload.items()}, 'files': {'image': ('source.jpg', images[0], 'image/jpeg')}}
        endpoint = '/images/edits'
    else:
        if images: raise HTTPException(422, '新建图片不能隐式使用原图，请选择编辑能力')
        kwargs, endpoint = {'json': payload}, '/images/generations'
    async with client.stream('POST', manifest.endpoint + endpoint, **kwargs) as response:
        response.raise_for_status()
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > 32 * 1024 * 1024: raise HTTPException(502, '图片模型响应超过限制')
        try: value = json.loads(data)
        except ValueError: raise HTTPException(502, '图片模型响应格式无效') from None
        if not isinstance(value, dict): raise HTTPException(502, '图片模型响应格式无效')
        return value
