"""真实 Agent 安卓验收：只点击已打开的第一方公开测试区域一次，并核对真实触点计数。"""
import argparse
import asyncio
import json
import re
import uuid
import xml.etree.ElementTree as ET

from fastapi import HTTPException
from sqlalchemy import select

from check_android_launch import task_guard
from homeai import agent, android_adb, android_workspace, runtime
from homeai.android_tools import AndroidAction, AndroidObservation
from homeai.android_tasks import TERMINAL, release_finished
from homeai.api import create_app
from homeai.client_actions import ClientAction, cancel_task_actions
from homeai.contracts import TaskRequest
from homeai.data import audit, emit
from homeai.db import Approval, Device, Invocation, Principal, Task, scope
from homeai.security import Actor


PACKAGE = 'dev.homeai.androidinput'
ACTIVITY = PACKAGE + '.TouchTestActivity'
SURFACE = 'gesture-test-surface'
GOAL = '我已绑定的安卓手机当前已经打开Home AI公开手势验收页。请先读取当前手机界面，在gesture-test-surface区域点击一次，再读取TOUCH_TEST计数确认。不要启动或切换任何应用，不输入文字，不访问其他资料。'
ACTIVE = {'RECEIVED', 'EXECUTING', 'APPROVED'}


class CheckStopped(Exception):
    """仅使用固定错误代号，避免模型内容、ADB 原始输出进入验收日志。"""


def binding(app, actor, *, expected_id=None, task_id=None):
    with app.db() as db:
        row = android_workspace.get_binding(app, db, actor)
        if not row or row.revoked or (expected_id is not None and row.id != expected_id):
            raise CheckStopped('binding_unavailable_or_changed')
        if row.mode == 'human':
            raise CheckStopped('human_control_active')
        if row.active_task_id and row.active_task_id != task_id:
            other = db.get(Task, row.active_task_id)
            if other and other.status not in TERMINAL:
                raise CheckStopped('another_android_task_active')
        packages = android_workspace.ai_packages(row)
        if packages != ['*'] and PACKAGE not in packages:
            raise CheckStopped('public_test_app_not_allowed')
        return row.id, row.serial


async def public_window(app, serial):
    """先核对解锁与精确前台组件，之后才允许读取公开测试页面。"""
    policy = android_adb.load_policy(app.settings)
    power = await android_adb._run(policy, serial, ['shell', 'dumpsys', 'power'], limit=512 * 1024)
    if re.search(rb'mWakefulness=Awake\b', power) is None:
        raise CheckStopped('android_not_awake')
    windows = await android_adb._run(policy, serial, ['shell', 'dumpsys', 'window'], limit=2 * 1024 * 1024)
    if re.search(rb'(?:mShowingLockscreen|mDreamingLockscreen|isStatusBarKeyguard|mKeyguardShowing|mKeyguardOccluded)=true\b', windows):
        raise CheckStopped('android_locked')
    match = re.search(rb'mCurrentFocus=Window\{[^}]*?\s(?:u\d+\s+)?([A-Za-z][A-Za-z0-9_.]+)/([^}\s]+)', windows)
    if not match:
        raise CheckStopped('foreground_unknown')
    package, activity = (value.decode('ascii') for value in match.groups())
    if activity.startswith('.'):
        activity = package + activity
    if package != PACKAGE or activity != ACTIVITY:
        raise CheckStopped('public_test_activity_not_foreground')


async def public_tree(app, serial):
    await public_window(app, serial)
    raw = await android_adb.hierarchy(app.settings, serial)
    await public_window(app, serial)
    if len(raw) > 512 * 1024 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise CheckStopped('invalid_public_test_tree')
    root = ET.fromstring(raw)
    nodes = [node for node in root.iter('node') if node.get('package') == PACKAGE]
    surfaces = [node for node in nodes if node.get('content-desc') == SURFACE and node.get('clickable') == 'true']
    counts = [re.search(r'\bTOUCH_TEST down=(\d+) up=(\d+)\b', node.get('text', '')) for node in nodes]
    counts = [match for match in counts if match]
    if len(surfaces) != 1 or len(counts) != 1:
        raise CheckStopped('public_test_surface_or_counter_missing')
    bounds = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', surfaces[0].get('bounds', ''))
    if not bounds:
        raise CheckStopped('public_test_surface_bounds_invalid')
    return {'down': int(counts[0].group(1)), 'up': int(counts[0].group(2))}, tuple(map(int, bounds.groups()))


def task_state(app, actor, task_id):
    with app.db() as db:
        scope(db, actor.user_id, actor.household_id)
        task = db.get(Task, task_id)
        body = app.vault.open(task.request, actor.user_id + ':task:' + task_id)
        rows = list(db.scalars(select(Invocation).where(Invocation.task_id == task_id).order_by(Invocation.step)))
        return task.status, body, rows


def bounded_context(body):
    # 直接 TaskRequest 不挂接持续会话；不得误读原聊天、附件、客户端快照或他人资料。
    if any(body.get(key) for key in ('record_ids', '_conversation_id', '_history_snapshot', '_mentions',
            '_parts', '_references', '_client_context', '_task_grants', '_historical_record_ids')):
        raise CheckStopped('unexpected_existing_context')


async def next_step(app, actor, task_id, workspace_id, serial, body, rows):
    bounded_context(body)
    binding(app, actor, expected_id=workspace_id, task_id=task_id)
    finished = {row.step for row in rows if row.status in {'SUCCEEDED', 'SKIPPED', 'UNAVAILABLE'}}
    waiting = [step for index, step in enumerate(body.get('steps', [])) if index not in finished]
    if not waiting:
        return False
    # 同一批中只要出现越界能力就整批停止，不能先执行前面的合法步骤。
    for proposal in waiting:
        if proposal.get('when') or proposal.get('capability') not in {'android.observe@v1', 'android.action@v1'}:
            raise CheckStopped('model_proposed_out_of_scope_tool')
        if proposal['capability'] == 'android.action@v1':
            values = proposal.get('arguments', {}).get('input', {})
            if not isinstance(values, dict) or values.get('action') != 'tap':
                raise CheckStopped('model_proposed_non_tap_action')
    pending = waiting[0]
    completed = {row.step: row for row in rows if row.status in {'SUCCEEDED', 'UNAVAILABLE'}}
    args = runtime.resolve_arguments(pending.get('arguments', {}), completed, app.vault, actor.user_id)
    await public_window(app, serial)
    if pending['capability'] == 'android.observe@v1':
        AndroidObservation.model_validate(args)
        return True
    action = AndroidAction.model_validate(args).input
    if action.action != 'tap':
        raise CheckStopped('model_proposed_non_tap_action')
    prior_actions = [row for row in rows if row.capability == 'android.action@v1']
    if any(row.status == 'SUCCEEDED' or body.get('_android_dispatched') == row.id
           or row.status in {'EXECUTING', 'NEEDS_RECONCILIATION'} for row in prior_actions):
        raise CheckStopped('tap_already_dispatched_no_replay')
    _, bounds = await public_tree(app, serial)
    observed = body.get('_android_observation', {})
    width, height = observed.get('width'), observed.get('height')
    if type(width) is not int or type(height) is not int or min(width, height) <= 0:
        raise CheckStopped('missing_observed_screen_size')
    x, y = action.x * width, action.y * height
    if not (bounds[0] <= x < bounds[2] and bounds[1] <= y < bounds[3]):
        raise CheckStopped('tap_outside_public_test_surface')
    # 此处只检查验收范围；真实目标、焦点、身份与派发仍由 Core 原路径重验。
    return True


def evidence(app, actor, task_id):
    status, body, rows = task_state(app, actor, task_id)
    with app.db() as db:
        scope(db, actor.user_id, actor.household_id)
        approvals = list(db.scalars(select(Approval).where(Approval.invocation_id.in_([row.id for row in rows]))))
        actions = list(db.scalars(select(ClientAction).where(ClientAction.task_id == task_id,
            ClientAction.requester_id == actor.user_id)))
    taps = [row for row in rows if row.capability == 'android.action@v1' and row.status == 'SUCCEEDED']
    later_observed = bool(taps and any(row.capability == 'android.observe@v1' and row.status == 'SUCCEEDED'
        and row.step > taps[0].step for row in rows))
    return {'task_status': status, 'tool_sequence': [{'step': row.step, 'capability': row.capability,
        'status': row.status} for row in rows], 'successful_taps': len(taps),
        'observed_after_tap': later_observed, 'approval_count': len(approvals),
        'client_action_count': len(actions), 'assistance_count': sum(row.kind == 'android.assist' for row in actions),
        'model_rounds': body.get('_model_rounds', 0), 'cloud_calls': body.get('_cloud_calls', 0)}


async def cleanup(app, actor, task_id):
    """仅取消本次未完成验收；保留结果不明与成功证据，不删除账本，不触碰其他任务。"""
    with app.db() as db:
        scope(db, actor.user_id, actor.household_id)
        task = db.get(Task, task_id)
        if task.status != 'SUCCEEDED':
            task.cancel_requested = True
            if task.status != 'NEEDS_RECONCILIATION':
                task.status = 'CANCELED'
            cancel_task_actions(app, db, actor, task)
            ids = select(Invocation.id).where(Invocation.task_id == task_id)
            for approval in db.scalars(select(Approval).where(Approval.invocation_id.in_(ids), Approval.decision == 'PENDING')):
                approval.decision = 'CANCELED'
            audit(db, actor, 'android.public_flow_check.stopped', task_id)
            emit(db, actor, 'task.updated', task_id)
            db.commit()
    await release_finished(app, task_id, actor.user_id)


async def check(app, member_id, device_id):
    report = {'task_id': None, 'passed': False, 'before': None, 'after': None,
        'stop_reason': None, 'read_existing_context': False, 'started_activity': False}
    actor, task_id = None, None
    try:
        with app.db() as db:
            member, device = db.get(Principal, member_id), db.get(Device, device_id)
            if not member or not device or device.revoked or device.user_id != member_id:
                raise CheckStopped('valid_paired_member_required')
            actor = Actor(member.id, member.household_id, device.id, member.role)
        workspace_id, serial = binding(app, actor)
        report['before'], _ = await public_tree(app, serial)
        with app.db() as db:
            task = runtime.submit(db, actor, TaskRequest(message=GOAL, mode='cloud', record_ids=[],
                idempotency_key='android-public-flow-' + str(uuid.uuid4()), max_steps=8,
                timeout_seconds=600, step_timeout_seconds=120, max_model_tokens=65536,
                max_output_tokens=512, max_read_retries=1), app.vault)
            task_id = task.id
            report['task_id'] = task_id
            # submit 已 flush；任务首次提交前取得与 Worker 相同的咨询锁。
            with task_guard(db.get_bind(), task_id):
                db.commit()
                try:
                    async with asyncio.timeout(600):
                        for _ in range(32):
                            binding(app, actor, expected_id=workspace_id, task_id=task_id)
                            await public_window(app, serial)
                            status, body, _ = task_state(app, actor, task_id)
                            bounded_context(body)
                            if status not in ACTIVE:
                                break
                            await agent.advance(app, task_id, actor.user_id)
                            status, body, rows = task_state(app, actor, task_id)
                            if status not in ACTIVE:
                                break
                            if await next_step(app, actor, task_id, workspace_id, serial, body, rows):
                                await runtime._run_step(app, task_id, actor.user_id)
                        else:
                            raise CheckStopped('check_iteration_limit')
                        report['after'], _ = await public_tree(app, serial)
                        report.update(evidence(app, actor, task_id))
                        report['passed'] = (report['task_status'] == 'SUCCEEDED' and report['successful_taps'] == 1
                            and report['observed_after_tap'] and report['approval_count'] == 0
                            and report['client_action_count'] == 0
                            and all(report['after'][key] - report['before'][key] == 1 for key in ('down', 'up')))
                        if not report['passed']:
                            report['stop_reason'] = 'core_waiting_or_failed' if report['task_status'] != 'SUCCEEDED' else 'real_touch_evidence_incomplete'
                finally:
                    try:
                        if report['after'] is None:
                            try:
                                report['after'], _ = await public_tree(app, serial)
                            except Exception:
                                report['after_unavailable'] = True
                        report.update(evidence(app, actor, task_id))
                    finally:
                        await asyncio.shield(cleanup(app, actor, task_id))
                    with app.db() as final_db:
                        scope(final_db, actor.user_id, actor.household_id)
                        row = android_workspace.get_binding(app, final_db, actor)
                        report['workspace_released'] = bool(row and row.active_task_id != task_id)
                        report['final_task_status'] = final_db.get(Task, task_id).status
    except CheckStopped as error:
        report['stop_reason'] = str(error)
    except (TimeoutError, asyncio.TimeoutError):
        report['stop_reason'] = 'check_timeout'
    except HTTPException as error:
        report['stop_reason'], report['http_status'] = 'core_http_error', error.status_code
    except Exception as error:
        report['stop_reason'], report['error_type'] = 'check_error', type(error).__name__
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--member-id', required=True)
    parser.add_argument('--device-id', required=True)
    args = parser.parse_args()
    result = asyncio.run(check(create_app().state, args.member_id, args.device_id))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['passed'] else 1)


if __name__ == '__main__':
    main()
