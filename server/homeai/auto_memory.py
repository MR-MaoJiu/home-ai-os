"""只对本人原文中的有限明确偏好自动记忆；分类不明确时不自动保存。"""
import json
import os
import re
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException
from sqlalchemy import String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from .contracts import DataRecord
from .crypto import canonical, digest
from .db import Base, Owned, ConversationTurn, MemoryCandidate, Principal, Record, now, scope
from .security import Actor, own
from .privacy import SECRET, PII
from .sync_order import lock_changes


class MemoryLearning(Owned, Base):
    __tablename__ = 'memory_learning'
    __table_args__ = (UniqueConstraint('owner_id', 'turn_id'),)
    turn_id: Mapped[str] = mapped_column(String)
    conversation_id: Mapped[str] = mapped_column(String)
    fingerprint: Mapped[str] = mapped_column(String, index=True)
    preference_key: Mapped[str] = mapped_column(String, index=True)
    polarity: Mapped[str] = mapped_column(String)
    classification: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    record_id: Mapped[str | None] = mapped_column(String, nullable=True)
    candidate_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    conflict_record_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[float] = mapped_column(default=now)


class MemoryForgetTombstone(Owned, Base):
    __tablename__ = 'memory_forget_tombstones'
    __table_args__ = (UniqueConstraint('owner_id', 'fingerprint'),)
    fingerprint: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[float] = mapped_column(default=now)


class MemoryForgetSource(Owned, Base):
    __tablename__ = 'memory_forget_sources'
    __table_args__ = (UniqueConstraint('owner_id','turn_id'),)
    turn_id: Mapped[str] = mapped_column(String,index=True)
    created_at: Mapped[float] = mapped_column(default=now)


def forget_source_turns(db,actor):
    scope(db,actor.user_id,actor.household_id)
    return set(db.scalars(select(MemoryForgetSource.turn_id).where(MemoryForgetSource.owner_id==actor.user_id,
        MemoryForgetSource.household_id==actor.household_id)))


def _forget_source(db,actor,turn_id):
    if not isinstance(turn_id,str) or not 1<=len(turn_id)<=100:raise ValueError('遗忘来源无效')
    if not db.scalar(select(MemoryForgetSource.id).where(MemoryForgetSource.owner_id==actor.user_id,MemoryForgetSource.turn_id==turn_id)):
        db.add(MemoryForgetSource(owner_id=actor.user_id,household_id=actor.household_id,turn_id=turn_id));db.flush()


# 该字典是明确、有限的语法范围，不宣称能够识别任意自然语言偏好。
ACTIVITIES = {'喝茶', '喝咖啡', '喝牛奶', '阅读', '读书', '跑步', '游泳', '徒步', '爬山', '旅行', '摄影', '听音乐', '看电影', '做饭', '下棋', '画画', '打篮球', '踢足球'}
QUOTED = re.compile(r'["“”「」『』]|(?:如果|假设|例如|示例|他说|她说|转发|引用|聊天记录|文件|照片|资料|以下|下面)')
SENSITIVE = re.compile(r'(?:病|健康|过敏|药|住址|住在|地址|电话|手机号码|邮箱|身份证|银行卡|收入|工资|宗教|政治|性取向)')


def parse_statement(content):
    if not isinstance(content,str) or not 1<=len(content)<=180 or '\n' in content or QUOTED.search(content) or SECRET.search(content):
        return None
    without_emails=PII.sub(lambda match:'' if match.lastgroup=='email' else match.group(0),content)
    if '@' in without_emails:return None
    normalized=content.strip().rstrip('。.!！').strip()
    normalized=re.sub(r'^(?:请记住|记住)[，,:：]?\s*','',normalized)
    if not normalized.startswith('我') or any(mark in normalized for mark in '?？；;'):
        return None
    if SENSITIVE.search(normalized) or PII.search(normalized):
        return {'content':content.strip(),'key':'sensitive:'+normalized,'polarity':'present','classification':'sensitive'}
    match=re.fullmatch(r'我(不)?喜欢(.+)',normalized)
    if match and match[2] in ACTIVITIES:
        return {'content':content.strip(),'key':'preference:'+match[2],'polarity':'negative' if match[1] else 'positive','classification':'ordinary'}
    match=re.fullmatch(r'我(?:更)?喜欢(?:用)?(中文|英文|英语)(?:回答|交流)',normalized)
    if match:
        return {'content':content.strip(),'key':'response_language','polarity':'中文' if match[1]=='中文' else '英文','classification':'ordinary'}
    return None


def statement_keys(actor, statement):
    key=digest(canonical([actor.user_id,statement['key']]))
    fingerprint=digest(canonical([actor.user_id,statement['key'],statement['polarity']]))
    return key,fingerprint


def content_fingerprint(actor,content):
    normalized=re.sub(r'\s+',' ',content.strip().rstrip('。.!！'))
    return digest(canonical([actor.user_id,'source-text',normalized]))


def _tombstone(db,actor,fingerprint):
    existing=db.scalar(select(MemoryForgetTombstone).where(MemoryForgetTombstone.owner_id==actor.user_id,MemoryForgetTombstone.fingerprint==fingerprint))
    if not existing:
        db.add(MemoryForgetTombstone(owner_id=actor.user_id,household_id=actor.household_id,fingerprint=fingerprint));db.flush()


def replay_forget_journal(db,vault,path,actor=None):
    """恢复旧备份时可独立重放；日志只存加密指纹，不存已遗忘正文。"""
    path=Path(path)
    if not path.exists():return 0
    if path.is_symlink() or not path.is_file():raise ValueError('记忆遗忘日志不能是符号链接')
    old=(db.info.get('user_id'),db.info.get('household_id'));count=0
    try:
        with path.open() as file:
            for line in file:
                value=vault.open(line.strip(),'memory-forget-journal')
                if actor and (value['owner_id']!=actor.user_id or value['household_id']!=actor.household_id):continue
                user=db.get(Principal,value['owner_id'])
                if not user or user.household_id!=value['household_id']:continue
                owner=Actor(user.id,user.household_id,'forget-replay',user.role)
                db.flush();scope(db,user.id,user.household_id)
                for fingerprint in value['fingerprints']:
                    if not re.fullmatch('[0-9a-f]{64}',fingerprint):raise ValueError('遗忘指纹无效')
                    _tombstone(db,owner,fingerprint);count+=1
                for turn_id in value.get('source_turn_ids',[]):_forget_source(db,owner,turn_id)
    finally:
        if old[0] and old[1]:scope(db,*old)
    return count


def _current_fact(app,db,actor,statement):
    key,_=statement_keys(actor,statement)
    rows=db.scalars(select(Record).where(Record.owner_id==actor.user_id,Record.kind=='memory.fact',Record.deleted.is_(False)))
    for record in rows:
        stored=app.vault.open(record.payload,record.owner_id+':record:'+record.id)
        parsed=parse_statement(stored.get('content',''))
        if parsed and statement_keys(actor,parsed)[0]==key:
            return record,parsed
    return None,None


def observe_turn(app,db,actor,turn,task_payload):
    """在创建用户聊天轮次后调用；不读取模型输出、附件正文或其他成员资料。"""
    scope(db,actor.user_id,actor.household_id);lock_changes(db)
    source=own(db,ConversationTurn,turn.id,actor)
    existing=db.scalar(select(MemoryLearning).where(MemoryLearning.owner_id==actor.user_id,MemoryLearning.turn_id==source.id))
    if existing:return {'status':existing.status,'record_id':existing.record_id,'candidate_id':existing.candidate_id}
    if any(task_payload.get(key) for key in ('_mentions','_task_grants','record_ids','_record_dependencies','parts','_media_parts','_attachments','_automation_id')):
        return {'status':'IGNORED'}
    raw=app.vault.open(source.user_message,actor.user_id+':chat-turn:'+source.id)
    statement=parse_statement(raw)
    if not statement:return {'status':'IGNORED'}
    try:replay_forget_journal(db,app.vault,app.settings.state_dir/'memory-forget.jsonl',actor)
    except Exception:return {'status':'UNAVAILABLE'}
    preference_key,fingerprint=statement_keys(actor,statement)
    if db.scalar(select(MemoryForgetTombstone.id).where(MemoryForgetTombstone.owner_id==actor.user_id,MemoryForgetTombstone.fingerprint.in_([fingerprint,content_fingerprint(actor,raw)]))):
        return {'status':'FORGOTTEN'}
    previous,previous_statement=_current_fact(app,db,actor,statement)
    identifier=str(uuid5(NAMESPACE_URL,'homeai:auto-memory:'+actor.user_id+':'+source.id))
    learning=MemoryLearning(id=identifier,owner_id=actor.user_id,household_id=actor.household_id,turn_id=source.id,
        conversation_id=source.conversation_id,fingerprint=fingerprint,preference_key=preference_key,
        polarity=statement['polarity'],classification=statement['classification'],status='PENDING')
    if previous and previous_statement['polarity']==statement['polarity']:
        learning.status,learning.record_id='DUPLICATE',previous.id;db.add(learning);db.flush()
        return {'status':'DUPLICATE','record_id':previous.id}
    conflict=previous is not None
    if statement['classification']=='sensitive' or conflict:
        from .memory import create_chat_candidate
        candidate=create_chat_candidate(app,db,actor,source.conversation_id,source.id,statement['content'],identifier)
        learning.status,learning.candidate_id='CANDIDATE',candidate.id
        learning.conflict_record_id=previous.id if conflict else None
        db.add(learning);db.flush()
        return {'status':'CANDIDATE','candidate_id':candidate.id,'reason':'conflict' if conflict else 'sensitive'}
    from .data import ingest,audit,emit
    record=ingest(db,actor,DataRecord(source='conversation_memory',source_id='auto:'+identifier,kind='memory.fact',version=1,
        sensitivity='PRIVATE',payload={'content':statement['content'],'source_ids':[],
            'conversation_id':source.conversation_id,'turn_id':source.id,'source_quote':statement['content'],
            'confirmed_at':now(),'origin':'automatic_preference','extraction_method':'literal_preference_v1'}),app.vault)
    learning.status,learning.record_id='SAVED',record.id;db.add(learning)
    audit(db,actor,'memory.preference_saved',record.id);emit(db,actor,'memory.updated',record.id);db.flush()
    return {'status':'SAVED','record_id':record.id}


def candidate_sensitivity(db,candidate_id,default='PRIVATE'):
    row=db.scalar(select(MemoryLearning).where(MemoryLearning.candidate_id==candidate_id))
    return 'SENSITIVE' if row and row.classification=='sensitive' else default


def before_confirm(app,db,actor,candidate):
    row=db.scalar(select(MemoryLearning).where(MemoryLearning.owner_id==actor.user_id,MemoryLearning.candidate_id==candidate.id))
    if not row:return
    if db.scalar(select(MemoryForgetTombstone.id).where(MemoryForgetTombstone.owner_id==actor.user_id,MemoryForgetTombstone.fingerprint==row.fingerprint)):
        raise HTTPException(409,'此内容已经被遗忘，不允许旧候选重新写入')
    if row.conflict_record_id:
        previous=db.get(Record,row.conflict_record_id)
        if previous and not previous.deleted:
            raise HTTPException(409,'候选与现有记忆冲突，请先删除不再准确的旧记忆，再确认新内容')


def bind_confirmed(db,actor,candidate,record):
    row=db.scalar(select(MemoryLearning).where(MemoryLearning.owner_id==actor.user_id,MemoryLearning.candidate_id==candidate.id))
    if row:
        row.status,row.record_id='CONFIRMED',record.id


def remember_forget(app,db,actor,record):
    """必须在删除记忆正文前调用，遗忘指纹不依赖随后可重建的向量索引。"""
    if record.owner_id!=actor.user_id or record.household_id!=actor.household_id or record.kind!='memory.fact' or record.deleted:
        return
    scope(db,actor.user_id,actor.household_id);lock_changes(db)
    rows=list(db.scalars(select(MemoryLearning).where(MemoryLearning.owner_id==actor.user_id,MemoryLearning.record_id==record.id)))
    fingerprints={row.fingerprint for row in rows}
    source_turns={row.turn_id for row in rows}
    payload=app.vault.open(record.payload,actor.user_id+':record:'+record.id)
    referenced=payload.get('turn_id')
    if referenced and db.scalar(select(ConversationTurn.id).where(ConversationTurn.id==referenced,ConversationTurn.owner_id==actor.user_id)):
        source_turns.add(referenced)
    content=payload.get('content','')
    if isinstance(content,str) and content:fingerprints.add(content_fingerprint(actor,content))
    parsed=parse_statement(content)
    if parsed:fingerprints.add(statement_keys(actor,parsed)[1])
    if not fingerprints:return
    journal=app.settings.state_dir/'memory-forget.jsonl';journal.parent.mkdir(parents=True,exist_ok=True)
    value=app.vault.seal({'owner_id':actor.user_id,'household_id':actor.household_id,'fingerprints':sorted(fingerprints),'source_turn_ids':sorted(source_turns)},'memory-forget-journal')
    descriptor=os.open(journal,os.O_WRONLY|os.O_APPEND|os.O_CREAT|os.O_NOFOLLOW,0o600)
    with os.fdopen(descriptor,'a') as file:
        file.write(value+'\n');file.flush();os.fsync(file.fileno())
    for fingerprint in fingerprints:_tombstone(db,actor,fingerprint)
    for turn_id in source_turns:_forget_source(db,actor,turn_id)
    for row in rows:row.status='FORGOTTEN'
