"""仅显式指定已授权设备时运行，操作系统设置搜索框，不打开消费 App。"""
import asyncio
import json
import os
import re
import xml.etree.ElementTree as ET
import pytest
from fastapi import HTTPException
from homeai import android_workspace as android, android_adb as adb
from homeai.security import Actor


@pytest.mark.skipif(not os.environ.get('HOMEAI_ANDROID_STREAM_SERIAL'), reason='需显式指定已授权安卓测试设备')
async def test_real_chinese_input_and_takeover(system, alice):
    app = system[0].state
    serial = os.environ['HOMEAI_ANDROID_STREAM_SERIAL']
    binary = os.environ['HOMEAI_ANDROID_ADB']
    policy = app.settings.state_dir / 'android-policy.json'
    policy.write_text(json.dumps({'enabled': True, 'adb_binary': binary, 'serials': [serial], 'ime_serials': [serial]})); policy.chmod(0o600)
    actor = Actor(alice.user_id, 'h1', alice.device_id, 'infrastructure_owner')
    with app.db() as db:
        db.add(android.AndroidWorkspace(owner_id=actor.user_id, household_id='h1', serial=serial,
            label='公开系统设置验收', backend='physical', packages='["com.android.settings"]'))
        db.commit()
    state = (await android.workspace(app, actor))['workspace']
    state = (await android.control(app, actor, android.ControlRequest(mode='human', expected_epoch=state['epoch'])))['workspace']
    sequence = 0
    async def action(name, **fields):
        nonlocal sequence, state
        state = (await android.control(app, actor, android.ControlRequest(mode='human', expected_epoch=state['epoch'])))['workspace']
        for retry in range(4):
            _, frame = await android.capture(app, actor)
            if name == 'text':
                claim = await android.prepare_input(app, actor, android.InputFocusRequest(epoch=state['epoch']))
                fields['focus_token'] = claim['focus_token']
            try:
                await android.input_event(app, actor, android.InputRequest(epoch=state['epoch'], sequence=sequence+1,
                    frame_id=frame['frame_id'], action=name, **fields))
                sequence += 1
                return
            except HTTPException as exc:
                # 仅明确尚未派发的旧画面拒绝可重新采样；实际输入失败不重放。
                if exc.status_code == 409 and '画面' in str(exc.detail) and retry < 3:
                    continue
                raise
    try:
        await action('launch', package='com.android.settings')
        await asyncio.sleep(.5)
        tree = ET.fromstring(await adb.hierarchy(app.settings, serial))
        node = next((n for n in tree.iter('node') if n.get('resource-id') == 'android:id/search_src_text'), None)
        assert node is not None, '此设备设置页搜索控件尚未适配，未点击任意未知位置'
        bounds = list(map(int, re.findall(r'\d+', node.get('bounds'))))
        _, frame = await android.capture(app, actor)
        await action('tap', x=(bounds[0]+bounds[2])/2/frame['width'], y=(bounds[1]+bounds[3])/2/frame['height'])
        await asyncio.sleep(.5)
        await action('text', text='家庭安卓验证')
        tree = await adb.hierarchy(app.settings, serial)
        assert b'\xe5\xae\xb6\xe5\xba\xad\xe5\xae\x89\xe5\x8d\x93\xe9\xaa\x8c\xe8\xaf\x81' in tree
        state = (await android.control(app, actor, android.ControlRequest(mode='ai', expected_epoch=state['epoch'])))['workspace']
        _, observed = await android.observe(app, actor)
        state = (await android.control(app, actor, android.ControlRequest(mode='human', expected_epoch=state['epoch'])))['workspace']
        with pytest.raises(HTTPException) as blocked:
            await android.input_event(app, actor, android.InputRequest(epoch=observed['epoch'], sequence=1,
                frame_id=observed['frame_id'], action='back'), source='ai')
        assert blocked.value.status_code == 409
        with pytest.raises(HTTPException):
            await android.observe(app, actor)
        sequence = 0
        await action('back')
        print('ANDROID_REAL_CONTROL_OK chinese_input=true takeover_rejects_old_ai=true third_party_orders=0')
    finally:
        state = (await android.workspace(app, actor))['workspace']
        await android.control(app, actor, android.ControlRequest(mode='paused', expected_epoch=state['epoch']))
