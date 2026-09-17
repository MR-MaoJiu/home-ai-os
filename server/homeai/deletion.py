"""删除原始来源时同步撤下引用该来源的候选与规范派生记忆。"""
import json
import os
from sqlalchemy import select, delete
from .db import Record, MemoryCandidate, MemoryVector, Revision, Grant, Task, Invocation, now
from .data import emit, audit


def delete_tree(db, actor, record, vault, state_dir):
    from .sync_order import lock_changes, notify_recipients, invalidate_snapshots
    lock_changes(db)
    # 与派生记录创建共用来源行锁，避免删除闭包计算期间出现新的解析正文。
    db.refresh(record, with_for_update=True)
    targets={record.id:record}
    # 规范事实也可能引用另一条规范事实，计算闭包防止多层来源残留。
    memories=db.scalars(select(Record).where(Record.owner_id==actor.user_id,Record.kind.in_(['memory.fact','document.parsed']),Record.deleted.is_(False))).all()
    while True:
        previous=len(targets)
        for item in memories:
            payload=vault.open(item.payload,actor.user_id+':record:'+item.id)
            if set(payload.get('source_ids',[])) & targets.keys():targets[item.id]=item
        if len(targets)==previous:break
    recipients=list(db.scalars(select(Grant.grantee_id).where(Grant.owner_id==actor.user_id,Grant.record_id.in_(targets))))
    invalidate_snapshots(db,actor,[actor.user_id,*recipients])
    journal=state_dir/'deletions.jsonl'
    journal.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(journal,os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)
    with os.fdopen(fd,'a') as file:
        for item in targets.values():
            file.write(vault.seal({'record_id':item.id,'owner_id':actor.user_id},'deletion-journal')+'\n')
        file.flush()
        os.fsync(file.fileno())
    for candidate in db.scalars(select(MemoryCandidate).where(MemoryCandidate.owner_id==actor.user_id)):
        if set(json.loads(candidate.source_ids)) & targets.keys():
            candidate.status='SOURCE_DELETED'
            candidate.content=vault.seal('',actor.user_id+':candidate:'+candidate.id)
    for item in targets.values():
        item.deleted,item.payload,item.updated_at=True,'',now()
        notify_recipients(db,actor,'record.deleted',item.id)
        db.flush()
        for table in (MemoryVector,Revision,Grant):
            db.execute(delete(table).where(table.record_id==item.id))
        emit(db,actor,'record.deleted',item.id)
        audit(db,actor,'data.delete',item.id)
    # 对话结果与工具结果可能复制私人数据；删除数据后保守清除该用户的执行结果缓存。
    for task in db.scalars(select(Task).where(Task.owner_id==actor.user_id)):
        task.result=None
        body=vault.open(task.request,actor.user_id+':task:'+task.id)
        if body.get('_agent'):
            # 规划草稿和模型生成的工具参数也可能复制来源内容，删除时一并撤下。
            body.pop('_draft_answer', None)
            body.pop('_agent_batches', None)
            body['steps'] = []
            task.request = vault.seal(body, actor.user_id + ':task:' + task.id)
        if body.get('_agent') or task.status == 'EXECUTING' or set(body.get('record_ids',[])) & targets.keys():
            task.cancel_requested=True
            if task.status in {'RECEIVED','APPROVED','AWAITING_APPROVAL'}:task.status='CANCELED'
    for invocation in db.scalars(select(Invocation).where(Invocation.owner_id==actor.user_id)):
        invocation.result=None
    db.commit()
    for rid in targets:
        (state_dir/'blobs'/rid).unlink(missing_ok=True)
    return list(targets)
