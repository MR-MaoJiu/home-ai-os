"""真实Core核验链路；设备仅在边界提供公开控件样例。"""
import copy
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from homeai import android_action_guard as guard, android_adb, android_tools, android_workspace as android
from homeai.db import Task, now, uid
from homeai.policy import capability_risk
from homeai.security import Actor
from test_android_workspace import workspace


def ui(label='打开详情', *, clock='12:00', extra='', resource='com.example.test:id/open'):
    return (f'<hierarchy rotation="0"><node text="{clock}" bounds="[0,0][100,20]"/>'
            f'<node text="{label}" resource-id="{resource}" package="com.example.test" clickable="true" bounds="[0,40][160,120]"/>{extra}</hierarchy>').encode()


async def prepared(workspace, monkeypatch, raw=None):
    app, actor, executed = workspace
    monkeypatch.setattr(android_adb, 'hierarchy', AsyncMock(return_value=raw or ui()))
    monkeypatch.setattr(android_adb, 'focus_signature', AsyncMock(side_effect=HTTPException(409, '没有焦点')))
    await android.control(app, actor, android.ControlRequest(mode='ai', expected_epoch=1))
    identifier = uid()
    body = {'record_ids': [], '_android_epoch': 2}
    with app.db() as db:
        task = Task(id=identifier, owner_id=actor.user_id, household_id=actor.household_id, idempotency_key=uid(),
                    request_hash='public', request=app.vault.seal(body, actor.user_id+':task:'+identifier), deadline=now()+300)
        db.add(task); db.commit()
        result = await android_tools.execute(app, db, actor, task, body, 'android.observe@v1', {'purpose': '公开界面回归'}, uid())
        task.request = app.vault.seal(body, actor.user_id+':task:'+identifier); db.commit()
    args = {'purpose': '打开公开详情', 'impact_summary': '查看公开页面', 'input': {'action': 'tap',
        'epoch': 2, 'sequence': 1, 'frame_id': result['frame_id'], 'x': 0.25, 'y': 0.125}}
    return app, actor, executed, identifier, body, args


async def assess(values):
    app, actor, _, identifier, body, args = values
    with app.db() as db:
        return await guard.preflight(app, db, actor, db.get(Task, identifier), body, args)


async def test_normal_target_bypasses_approval_only_with_core_evidence(workspace, monkeypatch):
    values = await prepared(workspace, monkeypatch)
    app, actor, executed, identifier, body, args = values
    proof = await assess(values)
    assert capability_risk('android.action@v1', args) == 3
    assert capability_risk('android.action@v1', args, assessment=proof) == 2
    await app.policy.check(actor, 'android.action@v1', arguments=args, assessment=proof)
    with app.db() as db:
        result = await android_tools.execute(app, db, actor, db.get(Task, identifier), body,
                    'android.action@v1', args, uid(), assessment=proof)
    assert result['status'] == 'succeeded'
    executed.assert_awaited_once()


@pytest.mark.parametrize('label', ['确认支付', '删除', '发送', '允许', '提交', '结算', '移除', '确认呼叫', '取消订单'])
async def test_actual_dangerous_targets_request_assistance(workspace, monkeypatch, label):
    values = await prepared(workspace, monkeypatch, ui(label))
    with pytest.raises(guard.AndroidAssistanceRequired):
        await assess(values)
    values[2].assert_not_awaited()


@pytest.mark.parametrize('label', ['拒绝', '不允许', '取消', '关闭', '返回', '支付设置'])
async def test_clear_navigation_and_denial_do_not_request_approval(workspace, monkeypatch, label):
    values = await prepared(workspace, monkeypatch, ui(label, resource='com.eg.android.AlipayGphone:id/payment_settings'))
    assert capability_risk('android.action@v1', values[-1], assessment=await assess(values)) == 2


async def test_password_page_does_not_capture_or_persist_source(workspace, monkeypatch):
    app, actor, executed = workspace
    monkeypatch.setattr(android_adb, 'hierarchy', AsyncMock(return_value=b'<hierarchy><node password="true" text="not-to-save"/></hierarchy>'))
    await android.control(app, actor, android.ControlRequest(mode='ai', expected_epoch=1))
    with pytest.raises(guard.AndroidAssistanceRequired) as error:
        await android.observe(app, actor)
    assert error.value.code == 'password_required'
    android_adb.screenshot.assert_not_awaited()


async def test_old_frame_revalidated_semantically_clock_and_unrelated_button_change(workspace, monkeypatch):
    extra = '<node text="倒计时1" resource-id="com.example.test:id/ad" clickable="true" package="com.example.test" bounds="[170,40][300,120]"/>'
    values = await prepared(workspace, monkeypatch, ui(extra=extra))
    app, actor, executed, identifier, body, args = values
    with app.db() as db:
        android.get_binding(app, db, actor).frame_at = now()-300
        db.commit()
    monkeypatch.setattr(android_adb, 'hierarchy', AsyncMock(return_value=ui(clock='12:01', extra=extra.replace('倒计时1', '倒计时2'))))
    proof = await assess(values)
    with app.db() as db:
        await android_tools.execute(app, db, actor, db.get(Task, identifier), body, 'android.action@v1', args, uid(), assessment=proof)
    executed.assert_awaited_once()


async def test_target_changes_after_preflight_never_clicks(workspace, monkeypatch):
    values = await prepared(workspace, monkeypatch)
    proof = await assess(values)
    app, actor, executed, identifier, body, args = values
    monkeypatch.setattr(android_adb, 'hierarchy', AsyncMock(return_value=ui('另一条目')))
    with app.db() as db, pytest.raises(guard.AndroidNeedsObservation):
        await android_tools.execute(app, db, actor, db.get(Task, identifier), body, 'android.action@v1', args, uid(), assessment=proof)
    assert '_android_dispatched' not in body
    executed.assert_not_awaited()


async def test_forged_mismatched_or_other_member_evidence_rejected(workspace, monkeypatch):
    values = await prepared(workspace, monkeypatch)
    proof = await assess(values)
    app, actor, _, _, _, args = values
    copied = copy.copy(proof)
    object.__setattr__(copied, '_seal', object())
    assert capability_risk('android.action@v1', args, assessment=copied) == 3
    changed = copy.deepcopy(args); changed['input']['x'] = 0.4
    assert capability_risk('android.action@v1', changed, assessment=proof) == 3
    for capability, identity in [('host.execute@v1', actor), ('android.action@v1', Actor('other', actor.household_id, actor.device_id, actor.role))]:
        with pytest.raises(HTTPException):
            await app.policy.check(identity, capability, arguments=args, assessment=proof)


async def test_human_takeover_invalidates_pending_evidence(workspace, monkeypatch):
    values = await prepared(workspace, monkeypatch)
    proof = await assess(values)
    app, actor, executed, _, _, args = values
    await android.control(app, actor, android.ControlRequest(mode='human', expected_epoch=2))
    with pytest.raises(HTTPException):
        await android.input_event(app, actor, android.InputRequest.model_validate(args['input']), source='ai', assessment=proof)
    executed.assert_not_awaited()


async def test_generic_confirmation_checks_actual_dialog_context(workspace, monkeypatch):
    raw = b'<hierarchy><node resource-id="android:id/parentPanel" class="android.app.AlertDialog"><node text="Delete file?"/><node text="OK" clickable="true" package="com.example.test" bounds="[0,40][160,120]"/></node></hierarchy>'
    values = await prepared(workspace, monkeypatch, raw)
    with pytest.raises(guard.AndroidAssistanceRequired) as error:
        await assess(values)
    assert error.value.code == 'sensitive_confirmation'


async def test_offline_device_is_assistance_not_permission_bypass(workspace, monkeypatch):
    values = await prepared(workspace, monkeypatch)
    monkeypatch.setattr(android_adb, 'devices', AsyncMock(return_value=[]))
    with pytest.raises(guard.AndroidAssistanceRequired) as error:
        await assess(values)
    assert error.value.code == 'device_offline'


async def test_nonpassword_input_renews_expired_claim_only_after_same_focus_check(workspace, monkeypatch):
    raw = b'<hierarchy><node class="android.widget.EditText" resource-id="com.example.test:id/search" text="public" focused="true" editable="true" package="com.example.test" bounds="[0,40][160,120]"/></hierarchy>'
    values = await prepared(workspace, monkeypatch, raw)
    app, actor, executed, identifier, body, args = values
    monkeypatch.setattr(android_adb, 'focus_signature', AsyncMock(return_value={'fingerprint': 'same-focus', 'password': False}))
    body['_android_observation']['focus_fingerprint'] = 'same-focus'
    args['input'].update(action='text', text='公开搜索词')
    args['input'].pop('x'); args['input'].pop('y')
    proof = await assess(values)
    with app.db() as db:
        await android_tools.execute(app, db, actor, db.get(Task, identifier), body, 'android.action@v1', args, uid(), assessment=proof)
        claims = db.query(android.AndroidInputFocus).all()
        assert len(claims) == 1 and claims[0].consumed
    executed.assert_awaited_once()


async def test_restored_current_task_old_model_epoch_requests_observation(workspace, monkeypatch):
    values = await prepared(workspace, monkeypatch)
    app, actor, _, _, body, args = values
    args['input']['epoch'] = 1
    with app.db() as db, pytest.raises(guard.AndroidNeedsObservation) as error:
        android_tools.prepare(app, db, actor, body, 'android.action@v1', args)
    assert error.value.code == 'action_stale'


async def test_risk_reason_mentions_only_actual_category(workspace, monkeypatch):
    values = await prepared(workspace, monkeypatch, ui('删除记录'))
    with pytest.raises(guard.AndroidAssistanceRequired) as error:
        await assess(values)
    assert error.value.reason == '本次目标涉及删除，请本人确认并操作'
    assert '付款' not in error.value.reason and '授权' not in error.value.reason


async def test_overlapping_sibling_controls_require_assistance(workspace, monkeypatch):
    extra = '<node text="覆盖窗口" resource-id="com.example.test:id/overlay" clickable="true" package="com.example.test" bounds="[0,20][300,300]"/>'
    values = await prepared(workspace, monkeypatch, ui(extra=extra))
    with pytest.raises(guard.AndroidAssistanceRequired) as error:
        await assess(values)
    assert error.value.code == 'target_ambiguous'
    values[2].assert_not_awaited()


async def test_nested_controls_choose_deepest_target_even_with_equal_bounds(workspace, monkeypatch):
    raw = b'<hierarchy><node clickable="true" resource-id="com.example.test:id/container" package="com.example.test" bounds="[0,40][160,120]"><node clickable="true" text="Open" resource-id="com.example.test:id/open" package="com.example.test" bounds="[0,40][160,120]"/></node></hierarchy>'
    values = await prepared(workspace, monkeypatch, raw)
    proof = await assess(values)
    app, actor, executed, identifier, body, args = values
    remembered = body['_android_observation']['controls']
    assert proof.target_fingerprint == max(remembered, key=lambda item: item['depth'])['fingerprint']
    with app.db() as db:
        await android_tools.execute(app, db, actor, db.get(Task, identifier), body, 'android.action@v1', args, uid(), assessment=proof)
    executed.assert_awaited_once()
