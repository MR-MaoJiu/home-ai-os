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
REMEMBER = re.compile(r'^(?:请)?(?:帮我)?(?:记住|记一下|记下来)[，,:：\s]*')
FOLLOWUP_REMEMBER = re.compile(r'^(?:请)?(?:记住(?:吧|这个|这些|刚才说的)?|记一下|记下来|保存(?:吧|记忆|这些)?|提交|重新提交)[。！!\s]*$')
NEXT_REQUEST = re.compile(r'[，,。；;\n]\s*(?=(?:(?:顺便|然后|另外|同时|并且|再)\s*)?(?:帮我|请|查一下|查查|搜索|打开|设置|创建|提醒|看看|告诉我|为什么|怎么|能不能|可以帮))')


def addressing(content):
    """只解析用户明确指定的称呼，保留“你”和“我”的方向。"""
    text=REMEMBER.sub('',content.strip()).strip('。.!！ ')
    text=re.sub(r'^(?:明明是|应该是|其实是)\s*','',text)
    text=re.sub(r'^(?:以后|今后|从现在开始)[，,\s]*','',text)
    text=re.sub(r'(?<=[^\s，,。；;])(?=你(?:可以|就)?叫我|我(?:以后|就)?叫你)','，',text)
    result={}
    parts=re.split(r'[，,。；;]|\s+(?:而|并且)\s*',text)
    for part in parts:
        part=re.sub(r'^(?:以后|今后|从现在开始)[，,\s]*','',part.strip())
        part=re.sub(r'(?:就行|就好|即可|吧)$','',part)
        patterns=[('preferred_name',r'(?:请|我希望)?(?:你(?:可以|就)?)?(?:以后)?叫我(.+)'),
                  ('assistant_name',r'(?:你(?:的名字(?:是|叫)|叫)|我(?:给你起(?:个|的)?名字叫|(?:以后|就)?叫你))(.+)'),
                  ('preferred_name',r'我(?:的名字是|叫)(.+)')]
        found=False
        for field,pattern in patterns:
            match=re.fullmatch(pattern,part)
            if not match:continue
            name=match[1].strip().strip('“”「」『』"')
            # 名称只作为显示值，不接受混入命令的长句或控制符。
            if not re.fullmatch(r'[\w\u3400-\u9fff· .-]{1,30}',name) or any(word in name for word in ('以后','叫我','记住','忽略','指令','执行','任务','帮我','你叫','我叫','打开','查看','点击','输入','删除','读取','搜索','外卖','打车','吃饭','睡觉','起床','回家')):
                return {}
            if field in result and result[field]!=name:return {}
            result[field]=name;found=True;break
        if not found:return {}
    return result


def memory_message(content):
    """拆分明确记忆和后续请求，不把待办事项吞成长期事实。"""
    if not isinstance(content,str):return None
    raw=content.strip();explicit=bool(REMEMBER.match(raw))
    trailing=re.search(r'[，,。；;]\s*(?:请)?(?:帮我)?(?:记住|记一下|记下来)[。！!\s]*$',raw)
    if trailing:raw=raw[:trailing.start()];explicit=True
    memory,remaining=raw,''
    split=NEXT_REQUEST.search(raw)
    if split and not addressing(raw) and (explicit or addressing(raw[:split.start()]) or parse_statement(raw[:split.start()])):
        memory,remaining=raw[:split.start()],raw[split.end():].strip()
    names=addressing(memory) if len(memory)<=180 and not SECRET.search(memory) else {}
    if names:
        statement={'content':REMEMBER.sub('',memory),'key':'addressing','polarity':canonical(names).decode(),'classification':'ordinary','addressing':names}
        # 称呼本身就是明确的个性化设置，不另要求“记住”按钮。
        explicit=True
    else:
        statement=parse_statement(memory)
        if not statement and explicit and len(memory)<=500 and not SECRET.search(memory) and not QUOTED.search(memory):
            value=REMEMBER.sub('',memory).strip().rstrip('。.!！')
            if value.startswith('我') and not any(mark in value for mark in '?？\n'):
                statement={'content':value,'key':'explicit:'+value,'polarity':'present','classification':'sensitive' if SENSITIVE.search(value) or PII.search(value) else 'explicit'}
    if not statement:return None
    return {'statement':statement,'explicit':explicit,'remaining_message':remaining,'handled_only':not remaining}


def parse_statement(content):
    if not isinstance(content,str) or not 1<=len(content)<=180 or '\n' in content or QUOTED.search(content) or SECRET.search(content):
        return None
    without_emails=PII.sub(lambda match:'' if match.lastgroup=='email' else match.group(0),content)
    if '@' in without_emails:return None
    normalized=content.strip().rstrip('。.!！').strip()
    normalized=REMEMBER.sub('',normalized)
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
    match=re.fullmatch(r'我(?:更)?(?:喜欢|希望)(?:你)?(?:回答|回复|说话)?(简短|简洁|详细)(?:一点|一些)?',normalized)
    if match:return {'content':content.strip(),'key':'response_length','polarity':'简短' if match[1] in {'简短','简洁'} else '详细','classification':'ordinary'}
    match=re.fullmatch(r'我(?:更)?(?:喜欢|希望)(?:你)?(?:回答|回复|说话|语气)?(温柔|轻松|直接|正式|口语化)(?:一点|一些)?',normalized)
    if match:return {'content':content.strip(),'key':'response_tone','polarity':match[1],'classification':'ordinary'}
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
    rows=db.scalars(select(Record).where(Record.owner_id==actor.user_id,Record.household_id==actor.household_id,Record.kind=='memory.fact',Record.deleted.is_(False)).order_by(Record.updated_at.desc(),Record.id))
    for record in rows:
        stored=app.vault.open(record.payload,record.owner_id+':record:'+record.id)
        if statement['classification']=='explicit' and stored.get('content','').strip()==statement['content'].strip():
            return record,statement
        observed=memory_message(stored.get('content',''))
        parsed=observed['statement'] if observed else None
        if parsed and statement_keys(actor,parsed)[0]==key:
            return record,parsed
    return None,None


def chat_source_context(app,db,actor,turn):
    """判断这一条本人原话的来源，不把无关旧任务的资料依赖带进来。"""
    from .db import Task
    if not turn.task_id:return {}
    task=own(db,Task,turn.task_id,actor)
    context=app.vault.open(task.request,actor.user_id+':task:'+task.id)
    if context.get('_task_grants') or context.get('_mentions') or context.get('_automation_id'):
        raise HTTPException(403,'其他成员资料或自动化结果不能写入本人聊天记忆')
    if any(part.get('type')!='text' for part in context.get('_parts',[]) if isinstance(part,dict)):
        raise HTTPException(403,'附件和任务数据不能转换为聊天记忆')
    ids=set(context.get('record_ids',[]))-set(context.get('_historical_record_ids',[]))
    ids.update(item.get('record_id') for item in context.get('_references',[]) if isinstance(item,dict))
    from .data import read_record
    for identifier in ids:
        if identifier and not read_record(db,actor,identifier).kind.startswith('memory.'):
            raise HTTPException(403,'资料引用和任务数据不能转换为聊天记忆，请在不引用资料的新消息中明确表达个人记忆内容')
    # 走到这里的引用至多是本人已有记忆，不把历史检索依赖当作当前声明的来源。
    return {**context,'record_ids':[],'_record_dependencies':{}}


def resolve_statement(app,db,actor,turn):
    """短确认只回指紧邻的本人声明，不使用助手生成的回答作为记忆。"""
    raw=app.vault.open(turn.user_message,actor.user_id+':chat-turn:'+turn.id)
    observation=memory_message(raw)
    source=turn
    if not observation and isinstance(raw,str) and FOLLOWUP_REMEMBER.fullmatch(raw.strip()):
        prior=list(db.scalars(select(ConversationTurn).where(ConversationTurn.owner_id==actor.user_id,
            ConversationTurn.household_id==actor.household_id,ConversationTurn.conversation_id==turn.conversation_id,
            ConversationTurn.sequence<turn.sequence).order_by(ConversationTurn.sequence.desc()).limit(4)))
        for previous in prior:
            previous_text=app.vault.open(previous.user_message,actor.user_id+':chat-turn:'+previous.id)
            if FOLLOWUP_REMEMBER.fullmatch(previous_text.strip()):continue
            observation=memory_message(previous_text)
            if observation and observation['handled_only']:
                chat_source_context(app,db,actor,previous)
                observation={**observation,'explicit':True};source=previous
            else:observation=None
            break
    return source,observation


def observe_turn(app,db,actor,turn,task_payload):
    """在创建用户聊天轮次后调用；不读取模型输出、附件正文或其他成员资料。"""
    scope(db,actor.user_id,actor.household_id);lock_changes(db)
    source=own(db,ConversationTurn,turn.id,actor)
    try:
        memory_source,observed=resolve_statement(app,db,actor,source)
    except HTTPException:return {'status':'IGNORED'}
    metadata={key:observed[key] for key in ('handled_only','remaining_message')} if observed else {}
    existing=db.scalar(select(MemoryLearning).where(MemoryLearning.owner_id==actor.user_id,MemoryLearning.turn_id==source.id))
    if existing:return {'status':existing.status,'record_id':existing.record_id,'candidate_id':existing.candidate_id,**metadata}
    if any(task_payload.get(key) for key in ('_mentions','_task_grants','record_ids','_record_dependencies','parts','_media_parts','_attachments','_automation_id')):
        return {'status':'IGNORED'}
    raw=app.vault.open(source.user_message,actor.user_id+':chat-turn:'+source.id)
    if not observed:return {'status':'IGNORED'}
    statement,explicit=observed['statement'],observed['explicit']
    source_quote=statement['content'];source_turn_ids=[memory_source.id]
    try:replay_forget_journal(db,app.vault,app.settings.state_dir/'memory-forget.jsonl',actor)
    except Exception:return {'status':'UNAVAILABLE'}
    preference_key,fingerprint=statement_keys(actor,statement)
    forgotten_source=memory_source.id in forget_source_turns(db,actor)
    if forgotten_source or (not explicit and db.scalar(select(MemoryForgetTombstone.id).where(MemoryForgetTombstone.owner_id==actor.user_id,MemoryForgetTombstone.fingerprint.in_([fingerprint,content_fingerprint(actor,raw)])))):
        return {'status':'FORGOTTEN',**metadata}
    previous,previous_statement=_current_fact(app,db,actor,statement)
    if previous and statement.get('addressing'):
        names={**previous_statement.get('addressing',{}),**statement['addressing']}
        previous_payload=app.vault.open(previous.payload,actor.user_id+':record:'+previous.id)
        source_turn_ids=list(dict.fromkeys([*previous_payload.get('source_turn_ids',[]),previous_payload.get('turn_id'),memory_source.id]))
        source_turn_ids=[value for value in source_turn_ids if isinstance(value,str)]
        statement={**statement,'addressing':names,'polarity':canonical(names).decode(),
            'content':'，'.join([*(['你的名字是'+names['assistant_name']] if names.get('assistant_name') else []),
                                *(['请叫我'+names['preferred_name']] if names.get('preferred_name') else [])])}
        preference_key,fingerprint=statement_keys(actor,statement)
    identifier=str(uuid5(NAMESPACE_URL,'homeai:auto-memory:'+actor.user_id+':'+source.id))
    learning=MemoryLearning(id=identifier,owner_id=actor.user_id,household_id=actor.household_id,turn_id=source.id,
        conversation_id=source.conversation_id,fingerprint=fingerprint,preference_key=preference_key,
        polarity=statement['polarity'],classification=statement['classification'],status='PENDING')
    if previous and previous_statement['polarity']==statement['polarity']:
        learning.status,learning.record_id='DUPLICATE',previous.id;db.add(learning);db.flush()
        return {'status':'DUPLICATE','record_id':previous.id,**metadata}
    conflict=previous is not None
    if not explicit and (statement['classification']=='sensitive' or conflict):
        from .memory import create_chat_candidate
        candidate=create_chat_candidate(app,db,actor,source.conversation_id,source.id,statement['content'],identifier)
        learning.status,learning.candidate_id='CANDIDATE',candidate.id
        learning.conflict_record_id=previous.id if conflict else None
        db.add(learning);db.flush()
        return {'status':'CANDIDATE','candidate_id':candidate.id,'reason':'conflict' if conflict else statement['classification'],**metadata}
    from .data import ingest,audit,emit
    record=ingest(db,actor,DataRecord(source=previous.source if explicit and previous else 'conversation_memory',source_id=previous.source_id if explicit and previous else 'auto:'+identifier,kind='memory.fact',version=previous.version+1 if explicit and previous else 1,
        sensitivity='SENSITIVE' if statement['classification']=='sensitive' else 'PRIVATE',payload={'content':statement['content'],'source_ids':[],
            'conversation_id':memory_source.conversation_id,'turn_id':memory_source.id,'source_quote':source_quote,'source_turn_ids':source_turn_ids,
            **({'addressing':statement['addressing']} if statement.get('addressing') else {}),
            'confirmed_at':now(),'origin':'explicit_chat' if explicit else 'automatic_preference','extraction_method':'literal_preference_v1'}),app.vault)
    learning.status,learning.record_id='SAVED',record.id;db.add(learning)
    audit(db,actor,'memory.preference_saved',record.id);emit(db,actor,'memory.updated',record.id);db.flush()
    return {'status':'SAVED','record_id':record.id,**metadata}


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
    for referenced in [payload.get('turn_id'),*payload.get('source_turn_ids',[])]:
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
