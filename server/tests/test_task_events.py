"""真实持久任务状态的 WebSocket 通知与设备授权验证。"""
import asyncio
import os
import pytest
from starlette.websockets import WebSocketDisconnect
from homeai.runtime import run_task
from homeai.db import Credential, now
from homeai.crypto import digest
from test_workflows import workflow, create
from conftest import SignedClient

pytestmark = pytest.mark.skipif(os.getenv('HOMEAI_INTEGRATION') != '1', reason='需要真实 PostgreSQL 和 OPA')


def connect(user, path, headers=None):
    return user.client.websocket_connect(path, headers=headers or user.headers('GET', path, b''))


def test_persistent_state_reconnect_and_no_sensitive_payload(workflow):
    app, user = workflow
    tid = create(user, [{'capability': 'reminder.create@v1', 'arguments': {'title': '正文不进入状态流'}}])
    path = f'/api/v1/tasks/{tid}/events'
    with connect(user, path) as socket:
        first = socket.receive_json()
        assert first['tasks'][0]['status'] == 'RECEIVED'
        asyncio.run(run_task(app, tid, user.user_id))
        result = socket.receive_json()
        assert result['tasks'][0]['status'] == 'SUCCEEDED'
        assert result['tasks'][0]['steps'] == [{'step': 0, 'status': 'SUCCEEDED'}]
        assert '正文' not in str(result) and 'result' not in result['tasks'][0]
    with connect(user, path) as socket:
        assert socket.receive_json()['tasks'][0]['status'] == 'SUCCEEDED'


def test_cross_member_signature_replay_and_revocation(workflow):
    app, user = workflow
    tid = create(user, [{'capability': 'calendar.search@v1'}])
    path = f'/api/v1/tasks/{tid}/events'
    other = SignedClient(user.client, app.db)
    with pytest.raises(WebSocketDisconnect):
        with connect(other, path):
            pytest.fail('不允许读取其他成员任务')
    headers = user.headers('GET', path, b'')
    with connect(user, path, headers) as socket:
        assert socket.receive_json()['tasks'][0]['id'] == tid
    with pytest.raises(WebSocketDisconnect):
        with connect(user, path, headers):
            pytest.fail('不得重放握手证明')
    with connect(user, path) as socket:
        socket.receive_json()
        assert user.request('DELETE', '/api/v1/devices/' + user.device_id).status_code == 200
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 4401


def test_expiry_closes_existing_stream_and_cookie_is_not_enough(workflow):
    app, user = workflow
    path = '/api/v1/events/tasks'
    with pytest.raises(WebSocketDisconnect):
        with user.client.websocket_connect(path):
            pytest.fail('匿名状态流必须拒绝')
    with connect(user, path) as socket:
        assert socket.receive_json()['tasks'] == []
        with app.db() as db:
            db.get(Credential, digest(user.token.encode())).expires_at = now() - 1
            db.commit()
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 4401
