"""派生附件的规范来源索引；授权不依赖附件正文里可复制的来源说明。"""
from fastapi import HTTPException
from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint, exists, or_, select
from sqlalchemy.orm import Mapped, aliased, mapped_column
from .db import Base, Owned, Outbox, Record


class ArtifactSource(Owned, Base):
    __tablename__ = 'artifact_sources'
    __table_args__ = (UniqueConstraint('record_id', 'source_id'),
                     CheckConstraint('source_version > 0', name='artifact_source_positive_version'),
                     CheckConstraint('record_id != source_id', name='artifact_source_not_self'))
    record_id: Mapped[str] = mapped_column(String, ForeignKey('data_records.id', ondelete='CASCADE'), index=True)
    source_id: Mapped[str] = mapped_column(String, index=True)
    source_version: Mapped[int] = mapped_column(Integer)


def sources_visible(actor, record_model=Record):
    """所有祖先已在创建时展开，读路径只需要一次有界的反向存在性检查。"""
    edge, source = aliased(ArtifactSource), aliased(Record)
    invalid = exists(select(edge.id).select_from(edge).outerjoin(source, source.id == edge.source_id).where(
        edge.record_id == record_model.id,
        or_(edge.owner_id != record_model.owner_id, edge.household_id != record_model.household_id,
            source.id.is_(None), source.deleted.is_(True), source.sensitivity == 'SECRET',
            source.version != edge.source_version, source.owner_id != record_model.owner_id,
            source.household_id != record_model.household_id,
            (source.owner_id != actor.user_id) & (source.visibility != 'family')))).correlate(record_model)
    return ~invalid


def bind_sources(db, actor, record, source_versions, vault):
    from .data import read_record
    if not isinstance(source_versions, dict) or len(source_versions) > 128:
        raise HTTPException(422, 'Shell 成果来源必须是有界的规范版本映射')
    if record.owner_id != actor.user_id or record.household_id != actor.household_id:
        raise HTTPException(403, '不能为其他成员登记 Shell 成果')
    expanded, checked = dict(source_versions), set()
    while pending := set(expanded) - checked:
        identifier = sorted(pending)[0]
        version = expanded[identifier]
        if not isinstance(identifier, str) or type(version) is not int or version <= 0 or identifier == record.id:
            raise HTTPException(422, 'Shell 成果来源版本无效')
        checked.add(identifier)
        source = read_record(db, actor, identifier)
        # 与规范删除事务共用行锁，避免完成文件写入后才发现来源已被删除。
        db.refresh(source, with_for_update=True)
        if source.owner_id != actor.user_id or source.household_id != actor.household_id:
            raise HTTPException(403, 'Shell 成果只能派生自本人成员的资料')
        if source.deleted or source.sensitivity == 'SECRET' or source.version != version:
            raise HTTPException(409, 'Shell 成果来源已删除、变化或成为秘密')
        ancestors = {row.source_id: row.source_version for row in db.scalars(select(ArtifactSource).where(
            ArtifactSource.record_id == source.id, ArtifactSource.owner_id == actor.user_id,
            ArtifactSource.household_id == actor.household_id))}
        if source.source == 'document_parse' and source.kind == 'document.parsed':
            from .documents import checked_payload
            payload = checked_payload(db, actor, source, vault)
            ancestors[source.source_id] = payload['source_version']
        for ancestor_id, ancestor_version in ancestors.items():
            if ancestor_id == record.id or (ancestor_id in expanded and expanded[ancestor_id] != ancestor_version):
                raise HTTPException(409, 'Shell 成果来源存在循环或版本冲突')
            expanded[ancestor_id] = ancestor_version
        if len(expanded) > 128:
            raise HTTPException(413, 'Shell 成果来源链超过限制')
    existing = {row.source_id: row.source_version for row in db.scalars(select(ArtifactSource).where(
        ArtifactSource.record_id == record.id, ArtifactSource.owner_id == actor.user_id))}
    if existing:
        if existing != expanded:
            raise HTTPException(409, '同一成果不能替换已登记的来源')
        return
    for identifier, version in expanded.items():
        db.add(ArtifactSource(owner_id=actor.user_id, household_id=actor.household_id,
            record_id=record.id, source_id=identifier, source_version=version))
    db.flush()


def notify_source_change(db, actor, source_id):
    """来源变化也发出派生成果变化事件，让已经缓存成果的客户端及时撤下。"""
    from .data import event_metadata
    from .sync_order import notify_recipients
    records = db.scalars(select(Record).join(ArtifactSource, ArtifactSource.record_id == Record.id).where(
        ArtifactSource.source_id == source_id, ArtifactSource.owner_id == actor.user_id,
        ArtifactSource.household_id == actor.household_id, Record.deleted.is_(False))).all()
    if records:
        records += db.scalars(select(Record).where(Record.source == 'document_parse', Record.kind == 'document.parsed',
            Record.source_id.in_([record.id for record in records]), Record.owner_id == actor.user_id,
            Record.household_id == actor.household_id, Record.deleted.is_(False))).all()
    for record in records:
        db.add(Outbox(owner_id=actor.user_id, household_id=actor.household_id, kind='record.changed',
            resource_id=record.id, **event_metadata(db, 'record.changed', record.id)))
        notify_recipients(db, actor, 'record.changed', record.id)
