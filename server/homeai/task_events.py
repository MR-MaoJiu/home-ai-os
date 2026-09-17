"""前台任务状态流：设备签名握手，连接期间重新鉴权，重连发送当前规范状态。"""
import asyncio
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from .crypto import digest
from .contracts import TaskStateNotification
from .db import Credential, Device, Principal, Task, Invocation, now, scope
from .security import authenticate, own

router = APIRouter(prefix='/api/v1', tags=['任务状态流'])


def snapshot(app, actor, token_digest, task_id):
    with app.db() as db:
        auth = db.get(Credential, token_digest)
        device = db.get(Device, actor.device_id)
        user = db.get(Principal, actor.user_id)
        if not auth or auth.kind != 'access' or auth.expires_at <= now() or auth.user_id != actor.user_id or auth.device_id != actor.device_id or not device or device.revoked or device.user_id != actor.user_id or not user or user.household_id != actor.household_id:
            raise HTTPException(401, '会话过期或设备授权已撤销')
        scope(db, actor.user_id, actor.household_id)
        if task_id:
            rows = [own(db, Task, task_id, actor)]
            has_more = False
        else:
            rows = list(db.scalars(select(Task).where(Task.owner_id == actor.user_id)
                .order_by(Task.created_at.desc(), Task.id).limit(101)))
            has_more, rows = len(rows) > 100, rows[:100]
        ids = [row.id for row in rows]
        steps = list(db.scalars(select(Invocation).where(Invocation.task_id.in_(ids)).order_by(Invocation.task_id, Invocation.step))) if ids else []
        return TaskStateNotification(type='task.snapshot', has_more=has_more, tasks=[
            {'id': row.id, 'status': row.status, 'cancel_requested': row.cancel_requested,
             'steps': [{'step': item.step, 'status': item.status} for item in steps if item.task_id == row.id]}
            for row in rows]).model_dump()


async def stream(socket, task_id=None):
    app = socket.app.state
    token = socket.headers.get('authorization', '')
    if not token.startswith('Bearer '):
        await socket.close(code=4401)
        return
    try:
        # 复用 HTTP 的签名、时间窗与一次性 nonce 校验；WebSocket 不接受浏览器 Cookie。
        request_scope = {**socket.scope, 'type': 'http', 'method': 'GET', 'scheme': 'https',
                         'homeai.body_digest': digest(b'')}
        actor = await authenticate(Request(request_scope))
        token_digest = digest(token.removeprefix('Bearer ').encode())
        current = snapshot(app, actor, token_digest, task_id)
    except HTTPException as exc:
        await socket.close(code=4401 if exc.status_code == 401 else 4403)
        return
    connections = getattr(app, 'task_event_connections', None)
    if connections is None:
        connections = app.task_event_connections = {}
    key = actor.device_id
    if connections.get(key, 0) >= 4:
        await socket.close(code=4429)
        return
    connections[key] = connections.get(key, 0) + 1
    try:
        await socket.accept()
        await socket.send_json(current)
        last_sent = now()
        while True:
            try:
                incoming = await asyncio.wait_for(socket.receive(), timeout=1)
                if incoming['type'] == 'websocket.disconnect':
                    return
                # 此连接是单向状态流，不接收正文或执行命令。
                await socket.close(code=4400)
                return
            except TimeoutError:
                pass
            try:
                latest = snapshot(app, actor, token_digest, task_id)
            except HTTPException as exc:
                await socket.close(code=4401 if exc.status_code == 401 else 4403)
                return
            if latest != current:
                await socket.send_json(latest)
                current, last_sent = latest, now()
            elif now() - last_sent >= 15:
                await socket.send_json(TaskStateNotification(type='heartbeat').model_dump())
                last_sent = now()
    except WebSocketDisconnect:
        pass
    finally:
        connections[key] -= 1
        if connections[key] == 0:
            del connections[key]


@router.websocket('/events/tasks')
async def all_tasks(socket: WebSocket):
    await stream(socket)


@router.websocket('/tasks/{task_id}/events')
async def one_task(socket: WebSocket, task_id: str):
    await stream(socket, task_id)
