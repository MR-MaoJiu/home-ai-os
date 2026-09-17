"""真实 Postfix/Dovecot 与独立 Provider 的协议、审批和幂等验证。"""
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
import httpx
import pytest
from sqlalchemy import select
from homeai.db import Provider, Secret, Principal, Invocation, scope, uid
from homeai.contracts import ProviderManifest
from homeai.runtime import run_task
from test_workflows import workflow, create

pytestmark = pytest.mark.skipif(os.getenv('HOMEAI_MAIL_TEST') != '1' or os.getenv('HOMEAI_INTEGRATION') != '1', reason='需要真实 PostgreSQL、OPA 和本机 Postfix/Dovecot 测试域')


@pytest.mark.asyncio
@pytest.mark.parametrize('security', ['tls', 'starttls'])
async def test_real_mail_approval_delivery_headers_and_dedup(workflow, tmp_path, security):
    app, user = workflow
    root = Path('state/mail-test').resolve()
    credentials = json.loads((root / 'credentials.json').read_text())
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
    token = secrets.token_urlsafe(32)
    config = {'MAIL_SUBJECT_ID': user.user_id, 'MAIL_USER': credentials['user'], 'MAIL_PASSWORD': credentials['password'],
        'MAIL_CA_FILE': str(root / 'tls/ca.pem'), 'MAIL_STATE_DIR': str(tmp_path / 'ledger'),
        'IMAP_HOST': 'localhost', 'IMAP_PORT': '55993' if security == 'tls' else '55143', 'IMAP_SECURITY': security,
        'SMTP_HOST': 'localhost', 'SMTP_PORT': '55465' if security == 'tls' else '55587', 'SMTP_SECURITY': security,
        'PROVIDER_SERVICE_TOKEN': token}
    config_path = tmp_path / 'provider.json'
    config_path.write_text(json.dumps(config)); config_path.chmod(0o600)
    process = subprocess.Popen([sys.executable, 'scripts/run_mail.py', '--config', str(config_path), '--port', str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    endpoint = f'http://127.0.0.1:{port}'
    provider_id = 'a.mail.' + uuid.uuid4().hex
    try:
        with httpx.Client(timeout=2, trust_env=False) as client:
            for _ in range(40):
                try:
                    response = client.get(endpoint + '/health', headers={'Authorization': 'Bearer ' + token})
                    if response.status_code == 200 and response.json().get('adapter') == 'mail': break
                except httpx.TransportError: pass
                if process.poll() is not None: raise AssertionError('Provider 未启动')
                time.sleep(0.1)
            else: raise AssertionError('Provider 未就绪')
        with app.db() as db:
            principal = db.get(Principal, user.user_id); scope(db, user.user_id, principal.household_id)
            secret_id = uid()
            db.add(Secret(id=secret_id, owner_id=user.user_id, household_id=principal.household_id, provider_id=provider_id,
                value=app.vault.seal(token, user.user_id + ':secret:' + secret_id)))
            manifest = ProviderManifest(id=provider_id, version='1.1.0', adapter='http', endpoint=endpoint,
                allowed_hosts=['127.0.0.1'], secret_id=secret_id, capabilities={'mail.send@v1': '/invoke/send', 'mail.search@v1': '/invoke/search', 'mail.read@v1': '/invoke/read'})
            db.add(Provider(id=provider_id, manifest=manifest.model_dump_json(), enabled=True)); db.commit()
        title = '真实邮件协议验收-' + uuid.uuid4().hex
        arguments = {'to': credentials['user'], 'subject': title, 'text': '仅投递到本机测试域，不向其他邮箱发送。'}
        tid = create(user, [{'capability': 'mail.send@v1', 'arguments': arguments}])
        await run_task(app, tid, user.user_id)
        assert user.request('GET', '/api/v1/tasks/' + tid).json()['status'] == 'AWAITING_APPROVAL'
        pending = user.request('GET', '/api/v1/approvals').json()[0]
        assert user.request('POST', '/api/v1/approvals/' + pending['id'], {'decision': 'APPROVED'}).status_code == 200
        await run_task(app, tid, user.user_id)
        sent = user.request('GET', '/api/v1/tasks/' + tid).json()
        assert sent['status'] == 'SUCCEEDED', sent
        assert sent['result']['status'] == 'accepted_by_smtp'
        with app.db() as db:
            scope(db, user.user_id, principal.household_id)
            invocation_id = db.scalar(select(Invocation.id).where(Invocation.task_id == tid))
        # 重启独立 Provider，验证去重来自磁盘账本而非内存缓存。
        process.terminate(); process.wait(timeout=10)
        process = subprocess.Popen([sys.executable, 'scripts/run_mail.py', '--config', str(config_path), '--port', str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with httpx.Client(timeout=2, trust_env=False) as probe_client:
            for _ in range(40):
                try:
                    if probe_client.get(endpoint + '/health', headers={'Authorization': 'Bearer ' + token}).status_code == 200: break
                except httpx.TransportError: pass
                time.sleep(0.1)
            else: raise AssertionError('Provider 重启失败')
        with httpx.Client(timeout=30, trust_env=False, headers={'Authorization': 'Bearer ' + token}) as client:
            repeated = client.post(endpoint + '/invoke/send', json={'subject_id': user.user_id, 'invocation_id': invocation_id, 'arguments': arguments})
            assert repeated.status_code == 200 and repeated.json()['deduplicated'] is True
            conflict = client.post(endpoint + '/invoke/send', json={'subject_id': user.user_id, 'invocation_id': invocation_id, 'arguments': {**arguments, 'text': '不同内容'}})
            assert conflict.status_code == 409
            denied = client.post(endpoint + '/invoke/search', json={'subject_id': 'another-user', 'invocation_id': uid(), 'arguments': {}})
            assert denied.status_code == 403
        for _ in range(20):
            search_id = create(user, [{'capability': 'mail.search@v1', 'arguments': {'query': title, 'limit': 50}}])
            await run_task(app, search_id, user.user_id)
            found = user.request('GET', '/api/v1/tasks/' + search_id).json()
            assert found['status'] == 'SUCCEEDED', found
            if found['result']['messages']: break
            import asyncio
            await asyncio.sleep(0.2)
        assert len(found['result']['messages']) == 1
        assert found['result']['messages'][0]['message_id'] == sent['result']['message_id']
        assert 'text' not in found['result']['messages'][0]
        message = found['result']['messages'][0]
        read_id = create(user, [{'capability': 'mail.read@v1', 'arguments': {'uid': message['uid'], 'uidvalidity': found['result']['uidvalidity']}}])
        await run_task(app, read_id, user.user_id)
        read = user.request('GET', '/api/v1/tasks/' + read_id).json()
        assert read['status'] == 'SUCCEEDED', read
        assert arguments['text'] in read['result']['text']
        stored = user.request('GET', '/api/v1/data/' + read['result']['record_id']).json()
        assert stored['sensitivity'] == 'PRIVATE' and stored['cloud_policy'] == 'LOCAL_ONLY'
        with httpx.Client(timeout=30, trust_env=False, headers={'Authorization': 'Bearer ' + token}) as client:
            invalid = client.post(endpoint + '/invoke/search', json={'subject_id': user.user_id, 'invocation_id': uid(), 'arguments': {'uidvalidity': found['result']['uidvalidity'] + 1}})
            assert invalid.status_code == 409
    finally:
        process.terminate()
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired: process.kill(); process.wait()
        with app.db() as db:
            row = db.get(Provider, provider_id)
            if row: row.enabled = False
            db.commit()
