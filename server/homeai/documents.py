"""附件解析只从授权加密文件读取，Provider 不获得宿主路径或数据库凭据。"""
import base64
from fastapi import HTTPException
from sqlalchemy import select
from .db import Record
from .data import read_record, serialize, ingest
from .contracts import DataRecord


def prepare(db, actor, arguments, app):
    if set(arguments) != {'record_id'} or not isinstance(arguments['record_id'], str):
        raise HTTPException(422, '文档解析必须引用已上传文件的 record_id')
    source = read_record(db, actor, arguments['record_id'])
    if source.owner_id != actor.user_id or source.kind != 'document.file' or source.sensitivity == 'SECRET':
        raise HTTPException(403, '只允许解析自己的非秘密附件')
    metadata = serialize(source, app.vault)['payload']
    path = app.settings.state_dir / 'blobs' / source.id
    if not path.is_file() or path.is_symlink():
        raise HTTPException(404, '附件文件不存在')
    content = app.vault.open(path.read_text(), actor.user_id + ':blob:' + source.id)
    try:
        decoded = base64.b64decode(content, validate=True)
    except Exception:
        raise HTTPException(422, '附件内容无效') from None
    if len(decoded) > app.settings.max_upload_bytes:
        raise HTTPException(413, '附件超过限制')
    return source, {'filename': metadata['name'], 'content_base64': content}


def persist(db, actor, source_id, source_version, result, app):
    from .sync_order import lock_changes
    lock_changes(db)
    source = read_record(db, actor, source_id)
    db.refresh(source, with_for_update=True)
    if source.deleted or source.version != source_version or source.sensitivity == 'SECRET':
        raise HTTPException(409, '解析期间来源发生变化，未保存结果')
    if not isinstance(result, dict) or not isinstance(result.get('markdown'), str) or not result['markdown'].strip():
        raise HTTPException(502, '解析器没有返回有效正文')
    filename = serialize(source, app.vault)['payload'].get('name', '文档')
    payload = {'name': filename + ' · 正文', 'markdown': result['markdown'], 'source_ids': [source.id], 'source_version': source.version, 'format': result.get('format'), 'parser_version': result.get('parser_version')}
    existing = db.scalar(select(Record).where(Record.owner_id == actor.user_id, Record.source == 'document_parse', Record.source_id == source.id).with_for_update())
    version = existing.version if existing else 1
    if existing and not existing.deleted and serialize(existing, app.vault)['payload'] != payload:
        version += 1
    record = ingest(db, actor, DataRecord(source='document_parse', source_id=source.id, kind='document.parsed', version=version, sensitivity=source.sensitivity, cloud_policy=source.cloud_policy, payload=payload), app.vault)
    from .visibility import change_record
    change_record(db,actor,record,source.visibility)
    return {'record_id': record.id, 'source_id': source.id, 'status': 'parsed', 'markdown': result['markdown']}


def checked_payload(db, actor, record, vault, payload=None):
    """完整解析正文必须仍对应当前可读的原件，不能把过期正文当作新版本。"""
    if payload is None:payload = serialize(record, vault)['payload']
    if record.kind == 'document.parsed' and record.source == 'document_parse':
        source = read_record(db, actor, record.source_id)
        if source.owner_id != record.owner_id or source.kind not in {'document.file', 'document.import', 'photo.file', 'video.file'}:
            raise HTTPException(404, '文档来源不可用')
        if payload.get('source_version') != source.version:
            raise HTTPException(409, '原文件已更新，解析正文需要重新生成；请查看原文件')
    return payload


def details(db, actor, identifier, app):
    from .data import accessible
    from .sync_order import lock_changes
    lock_changes(db)
    source = read_record(db, actor, identifier)
    if source.kind not in {'document.file', 'document.import', 'photo.file', 'video.file'}:
        raise HTTPException(422, '此记录不是文档文件')
    parsed = db.scalar(accessible(db, actor).where(Record.owner_id == source.owner_id,
        Record.source == 'document_parse', Record.source_id == source.id, Record.kind == 'document.parsed'))
    if parsed is None:
        return {'record_id': source.id, 'parsed': None, 'parse_status': 'not_available'}
    value=serialize(parsed,app.vault)
    try:
        checked_payload(db, actor, parsed, app.vault,value['payload'])
    except HTTPException as exc:
        if exc.status_code == 409:
            return {'record_id': source.id, 'parsed': None, 'parse_status': 'stale'}
        raise
    return {'record_id': source.id, 'parsed': value, 'parse_status': 'ready'}


def content_type(contents, filename):
    """实际文件签名优先；下载始终attachment，网页及脚本不以内联方式执行。"""
    import mimetypes
    if contents.startswith(b'\xff\xd8\xff'): return 'image/jpeg'
    if contents.startswith(b'\x89PNG\r\n\x1a\n'): return 'image/png'
    if contents.startswith((b'GIF87a', b'GIF89a')): return 'image/gif'
    if contents.startswith(b'%PDF-'): return 'application/pdf'
    if contents[:4] == b'RIFF' and contents[8:12] == b'WEBP': return 'image/webp'
    return mimetypes.guess_type(filename)[0] or 'application/octet-stream'


def legacy_file(db, actor, record_id, app):
    """旧附件仍通过规范记录鉴权，供普通和任务范围下载共用。"""
    import base64
    record=read_record(db,actor,record_id)
    metadata=serialize(record,app.vault)['payload']
    if record.kind in {'document.import','photo.selected'}:
        encoded=metadata.get('content_base64','')
    elif record.kind=='document.file' and record.source!='media_upload':
        path=app.settings.state_dir/'blobs'/record.id
        if not path.is_file() or path.is_symlink():raise HTTPException(404,'文件不可用')
        encoded=app.vault.open(path.read_text(),record.owner_id+':blob:'+record.id)
    else:raise HTTPException(422,'此记录不是旧格式附件')
    try:contents=base64.b64decode(encoded,validate=True)
    except Exception:raise HTTPException(422,'文件内容无效') from None
    if len(contents)>app.settings.max_upload_bytes:raise HTTPException(413,'文件超过限制')
    filename=metadata.get('name','attachment')
    filename=filename[:200] if isinstance(filename,str) else 'attachment'
    read_record(db,actor,record_id)
    return contents,filename
