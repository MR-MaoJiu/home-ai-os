"""公开最小验收：真实模型选择打开系统设置或显式仅测 Core；不读取旧聊天、记忆或页面正文。"""
import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import json
import uuid
from fastapi import HTTPException
from sqlalchemy import text
from homeai.api import create_app
from homeai.android_tools import TOOLS, prepare
from homeai.android_workspace import get_binding, ai_packages
from homeai.cloud_gateway import invoke as cloud_invoke
from homeai.contracts import TaskRequest
from homeai.data import emit
from homeai.db import Principal, Device, Task, scope
from homeai.model_routing import resolve_role
from homeai.planner import decode_proposals
from homeai.runtime import submit, run_task
from homeai.security import Actor


@contextmanager
def task_guard(engine, task_id):
    """与 runtime.run_task 使用同一连接级咨询锁，在首次提交前阻止 Worker 抢占。"""
    if engine.dialect.name != 'postgresql':
        # 项目 SQLite 仅用于自动化测试；实际家庭服务必须使用 PostgreSQL。
        yield
        return
    key = int.from_bytes(hashlib.sha256(('task:' + task_id).encode()).digest()[:8], 'big', signed=True)
    connection = engine.connect()
    locked = False
    try:
        locked = connection.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': key}) is True
        if not locked:
            raise HTTPException(409, '公开验收任务已被另一执行器持有，未调用模型')
        connection.commit()
        yield
    finally:
        try:
            if locked:
                try:
                    connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': key})
                    connection.commit()
                except BaseException:
                    # 不把仍持有会话锁的连接送回池；断开连接由 PostgreSQL 释放会话锁。
                    connection.invalidate()
                    raise
        finally:
            connection.close()


async def check(app, member_id, device_id, *, skip_model=False):
    fixed = '只打开绑定安卓设备的系统设置（com.android.settings）并确认其位于前台；不读取页面内容，不观察界面，不点击设置项，不执行其他操作。'
    planning_id, provider_id = None, None
    with app.db() as db:
        member, device = db.get(Principal, member_id), db.get(Device, device_id)
        if not member or not device or device.revoked or device.user_id != member_id:
            raise HTTPException(403, '需要本人成员的真实有效配对设备')
        actor = Actor(member.id, member.household_id, device.id, member.role)
        row = get_binding(app, db, actor)
        if not row or row.mode == 'human':
            raise HTTPException(409, '设备未绑定或本人正在接管；验收不会抢占操作权')
        if ai_packages(row) != ['*'] and 'com.android.settings' not in ai_packages(row):
            raise HTTPException(403, '当前AI范围没有系统设置，验收不会扩大允许范围')
        if skip_model:
            checked = prepare(app, db, actor, {}, 'android.launch@v1', {
                'package': 'com.android.settings', 'purpose': fixed})
        else:
            planning = submit(db, actor, TaskRequest(idempotency_key='android-public-planning-' + str(uuid.uuid4()),
                capability='model.generate@v1', message=fixed, record_ids=[], max_steps=1, max_output_tokens=512,
                timeout_seconds=180), app.vault)
            planning_id = planning.id
            # submit 只 flush，其他连接仍不可见；取得真实任务锁后才提交 EXECUTING。
            with task_guard(db.get_bind(), planning_id):
                planning.status = 'EXECUTING'
                db.commit()
                body = None
                try:
                    body = app.vault.open(planning.request, actor.user_id + ':task:' + planning_id)
                    model = resolve_role(app, db, actor, 'cloud_planner')
                    provider_id = model.id
                    arguments = {'messages': [{'role': 'system', 'content': '只为公开测试选择工具。只能调用提供的open_android_app一次，包名必须是com.android.settings，不添加其他操作。'},
                        {'role': 'user', 'content': fixed}], 'tools': [item for item in TOOLS if item['function']['name'] == 'open_android_app'],
                        'max_tokens': 512, 'temperature': 0}
                    await app.policy.check(actor, 'model.generate@v1')
                    async with asyncio.timeout(150):
                        result = await cloud_invoke(app, db, actor, model, planning, body, arguments,
                            planning_id + ':public-model-selection') if model.cloud else await app.registry.invoke(
                                db, actor, model, 'model.generate@v1', arguments, planning_id + ':public-model-selection')
                    db.refresh(planning)
                    if planning.cancel_requested:
                        raise HTTPException(409, '公开规划验收已被取消，未执行设备操作')
                    proposals = decode_proposals(result, max_calls=1, allowed_names={'open_android_app'})
                    if len(proposals) != 1 or proposals[0][0] != 'android.launch@v1' or proposals[0][1].get('package') != 'com.android.settings':
                        raise HTTPException(409, '真实模型没有给出所要求的唯一打开工具；未执行设备操作')
                    checked = prepare(app, db, actor, {}, 'android.launch@v1', proposals[0][1])
                    planning.status = 'SUCCEEDED'
                    planning.result = app.vault.seal({'phase': 'model_tool_selection', 'tool': 'open_android_app',
                        'package': checked['package'], 'device_executed': False}, actor.user_id + ':task-result:' + planning_id)
                except BaseException as error:
                    planning.status = 'CANCELED' if planning.cancel_requested else 'FAILED'
                    planning.error = str(error.detail) if isinstance(error, HTTPException) else type(error).__name__
                    raise
                finally:
                    if body is not None:
                        planning.request = app.vault.seal(body, actor.user_id + ':task:' + planning_id)
                    emit(db, actor, 'task.updated', planning_id)
                    db.commit()
        execution = submit(db, actor, TaskRequest(idempotency_key='android-public-execution-' + str(uuid.uuid4()),
            capability='android.launch@v1', arguments=checked, record_ids=[], timeout_seconds=90), app.vault)
        execution_id = execution.id
        db.commit()
    await run_task(app, execution_id, actor.user_id)
    for _ in range(120):
        with app.db() as db:
            scope(db, actor.user_id, actor.household_id)
            execution = db.get(Task, execution_id)
            if execution.status in {'SUCCEEDED', 'FAILED', 'CANCELED', 'NEEDS_RECONCILIATION'}:
                outcome = app.vault.open(execution.result, actor.user_id + ':task-result:' + execution_id) if execution.result else {}
                row = get_binding(app, db, actor)
                return {'mode': 'core_only' if skip_model else 'model_and_core',
                    'planning_task_id': planning_id, 'execution_task_id': execution_id,
                    'model_selected_tool': None if skip_model else 'open_android_app', 'provider_id': provider_id,
                    'execution_status': execution.status, 'foreground_verified': outcome.get('foreground_verified') is True,
                    'workspace_released': bool(row and row.active_task_id is None and row.mode == 'paused'),
                    'error': execution.error, 'read_existing_context': False, 'read_page_content': False}
        await asyncio.sleep(.25)
    raise HTTPException(504, '真实执行尚未返回终态，请按任务标识查询；不能重复提交启动')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--member-id', required=True)
    parser.add_argument('--device-id', required=True)
    parser.add_argument('--skip-model', action='store_true', help='仅验收Core真实启动，不调用模型，不把Core成功报告为模型成功')
    args = parser.parse_args()
    print(json.dumps(asyncio.run(check(create_app().state, args.member_id, args.device_id,
        skip_model=args.skip_model)), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
