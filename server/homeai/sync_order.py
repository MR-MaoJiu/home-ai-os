"""家用规模的数据事务顺序边界，防止事件 ID 先分配后提交造成游标漏读。"""
from sqlalchemy import text,select,insert
from .db import Grant,Outbox,uid,now


def lock_changes(db):
    if db.get_bind().dialect.name=='postgresql':
        db.execute(text('SELECT pg_advisory_xact_lock(721934805)'))


def notify_recipients(db,actor,kind,record_id,recipients=None):
    if recipients is None:
        recipients=list(db.scalars(select(Grant.grantee_id).where(Grant.owner_id==actor.user_id,Grant.record_id==record_id)))
    for recipient in set(recipients)-{actor.user_id}:
        # INSERT 不请求 RETURNING，发送者不因此获得读取接收者事件的权限。
        db.flush()
        db.connection().execute(insert(Outbox).inline().values(event_id=uid(),owner_id=recipient,household_id=actor.household_id,kind=kind,resource_id=record_id,published=False,created_at=now()))


def invalidate_snapshots(db,actor,recipients):
    """删除服务端缓存；身份仅来自已经验证的源所有者与共享成员集合。"""
    from .db import SyncSnapshot
    from sqlalchemy import delete
    db.flush()
    with db.no_autoflush:
        connection=db.connection()
        try:
            for recipient in set(recipients):
                if connection.dialect.name=='postgresql':
                    connection.execute(text("SELECT set_config('homeai.user_id',:user,true),set_config('homeai.household_id',:household,true)"),{'user':recipient,'household':actor.household_id})
                connection.execute(delete(SyncSnapshot).where(SyncSnapshot.owner_id==recipient,SyncSnapshot.household_id==actor.household_id))
        finally:
            if connection.dialect.name=='postgresql':
                connection.execute(text("SELECT set_config('homeai.user_id',:user,true),set_config('homeai.household_id',:household,true)"),{'user':actor.user_id,'household':actor.household_id})
