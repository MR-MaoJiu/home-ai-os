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
    return {'record_id': record.id, 'source_id': source.id, 'status': 'parsed', 'markdown': result['markdown']}
