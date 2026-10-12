"""服务端取得的资源统一进入加密附件账本，不把临时路径或远程下载凭据交给客户端。"""
import math
import mimetypes
import uuid
from fastapi import HTTPException
from .crypto import digest, canonical
from .contracts import DataRecord
from .db import now
from .data import ingest, read_record, audit, emit
from .media import (MediaUpload, MediaChunk, MediaJob, UploadInput, CHUNK_SIZE,
                    directory, write_chunk, probe, limits_for, checked_metadata)


def part_for(app, db, actor, record, *, task_id=None):
    metadata = checked_metadata(app, record)
    provenance = metadata.get('provenance', {})
    return {'type': metadata['kind'], 'record_id': record.id, 'version': record.version,
            'title': metadata['name'], **({'provenance': provenance} if provenance else {}),
            **({'task_id': task_id} if task_id else {})}


def store_asset(app, db, actor, task, invocation_id, raw, name, kind, provenance):
    maximum = limits_for(app).get(kind, 0)
    if not isinstance(raw, bytes) or not 0 < len(raw) <= maximum:
        raise HTTPException(413, '返回资源为空或超过附件大小限制')
    fingerprint = digest(raw)
    UploadInput(client_id=uuid.uuid5(uuid.NAMESPACE_URL, invocation_id), name=name,
                kind=kind, mime_type='application/octet-stream', size=len(raw), sha256=fingerprint)
    identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, actor.user_id + ':resource:' + invocation_id))
    existing = db.get(MediaUpload, identifier)
    if existing:
        if existing.owner_id != actor.user_id or existing.sha256 != fingerprint or not existing.record_id:
            raise HTTPException(409, '同一资源步骤已有不同或不完整的结果，禁止覆盖')
        record = read_record(db, actor, existing.record_id)
        if provenance.get('origin') == 'shell_created':
            metadata = checked_metadata(app, record)
            if metadata.get('provenance', {}).get('source_versions', {}) != provenance.get('source_versions', {}):
                raise HTTPException(409, '同一 Shell 成果不能替换原始来源')
        return {'status': 'available', 'parts': [part_for(app, db, actor, record)], 'text': '资源已准备好。'}
    inspected = probe(raw, kind, max_bytes=maximum, max_seconds=app.settings.media_video_max_seconds)
    thumbnail = inspected.pop('thumbnail', None)
    if provenance.get('origin') not in {'generated', 'edited', 'retrieved', 'script_retrieved', 'shell_created', 'server_capture'}:
        raise HTTPException(422, '资源来源类型无效')
    metadata = {'name': name, 'kind': kind, 'size': len(raw), 'sha256': fingerprint,
                'media_upload_id': identifier, **inspected, 'provenance': provenance,
                'mime_type': inspected.get('mime_type') or mimetypes.guess_type(name)[0] or 'application/octet-stream'}
    row = MediaUpload(id=identifier, owner_id=actor.user_id, household_id=actor.household_id,
                      client_id=identifier, request_hash=digest(canonical(metadata)), device_id=actor.device_id,
                      kind=kind, size=len(raw), sha256=fingerprint, status='completed', expires_at=now()+86400,
                      metadata_json=app.vault.seal(metadata, actor.user_id + ':upload:' + identifier))
    db.add(row)
    for index in range(math.ceil(len(raw) / CHUNK_SIZE)):
        chunk = raw[index*CHUNK_SIZE:(index+1)*CHUNK_SIZE]
        write_chunk(app, row, index, chunk)
        db.add(MediaChunk(owner_id=actor.user_id, household_id=actor.household_id, upload_id=identifier,
                          position=index, size=len(chunk), sha256=digest(chunk)))
    folder = directory(app, identifier, create=True)
    if thumbnail:
        path = folder / 'thumbnail.enc'
        path.write_text(app.vault.seal(thumbnail, actor.user_id + ':thumbnail:' + identifier)); path.chmod(0o600)
    record = ingest(db, actor, DataRecord(source='media_upload', source_id=identifier,
        kind={'image':'photo.file', 'video':'video.file', 'file':'document.file'}[kind], version=1,
        sensitivity='PRIVATE', cloud_policy='LOCAL_ONLY', payload=metadata), app.vault)
    if provenance.get('origin') == 'shell_created':
        from .artifact_sources import bind_sources
        bind_sources(db, actor, record, provenance.get('source_versions', {}), app.vault)
    path = folder / 'manifest.enc'
    path.write_text(app.vault.seal({'record_id': record.id, 'version': record.version, 'metadata': metadata},
                                 actor.user_id + ':media-manifest:' + identifier)); path.chmod(0o600)
    row.record_id = record.id
    db.add(MediaJob(owner_id=actor.user_id, household_id=actor.household_id, record_id=record.id,
                    upload_id=identifier, device_id=actor.device_id, version=record.version))
    audit(db, actor, 'resource.available', record.id, {'origin': provenance['origin']})
    emit(db, actor, 'media.queued', record.id)
    db.flush()
    return {'status': 'available', 'parts': [part_for(app, db, actor, record)], 'text': '资源已准备好。'}


def send_existing(app, db, actor, body, args):
    if set(args) != {'record_id'} or not isinstance(args.get('record_id'), str):
        raise HTTPException(422, '发送已有资料必须指定规范记录')
    from .client_actions import read_authorized_record
    record = read_authorized_record(db, actor, args['record_id'], body)
    if record.sensitivity == 'SECRET':
        raise HTTPException(403, '秘密资料不能进入聊天附件')
    temporary = record.id in body.get('_task_grants', {})
    return {'status': 'available', 'parts': [part_for(app, db, actor, record,
            task_id=body.get('_task_id') if temporary else None)], 'text': '已找到获准访问的资料。'}
