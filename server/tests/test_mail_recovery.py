"""真实认证失败后的邮件账本恢复与跨进程锁，不伪造 SMTP 响应。"""
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from homeai_providers.mail_adapter import operation
from homeai_providers.mail_recovery import inspect_delivery, resolve_delivery, delivery_lock

pytestmark = pytest.mark.skipif(os.getenv('HOMEAI_MAIL_TEST') != '1', reason='需要本机真实 Postfix/Dovecot 测试域')


def test_real_failure_reconcile_and_single_retry(monkeypatch, tmp_path):
    root = Path('state/mail-test').resolve()
    account = json.loads((root / 'credentials.json').read_text())
    subject, invocation = str(uuid.uuid4()), str(uuid.uuid4())
    for key, value in {'MAIL_STATE_DIR': str(tmp_path), 'MAIL_SUBJECT_ID': subject,
        'MAIL_USER': account['user'], 'MAIL_PASSWORD': 'incorrect-test-password',
        'MAIL_CA_FILE': str(root / 'tls/ca.pem'), 'SMTP_HOST': 'localhost', 'SMTP_PORT': '55465',
        'SMTP_SECURITY': 'tls'}.items(): monkeypatch.setenv(key, value)
    call = SimpleNamespace(subject_id=subject, invocation_id=invocation,
        arguments={'to': account['user'], 'subject': '本机人工核对-' + invocation, 'text': '只投递到本机测试域'})
    import smtplib
    with pytest.raises(smtplib.SMTPAuthenticationError): operation('send', call)
    row = inspect_delivery(subject, invocation)
    assert row['status'] == 'UNCERTAIN'
    monkeypatch.setenv('MAIL_PASSWORD', account['password'])
    with pytest.raises(HTTPException) as blocked: operation('send', call)
    assert blocked.value.status_code == 409
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'MAIL_STATE_DIR': str(tmp_path), 'MAIL_SUBJECT_ID': subject})); config.chmod(0o600)
    evidence = tmp_path / 'evidence.txt'
    evidence.write_text('实际 SMTP 返回认证失败，未进入 MAIL FROM、RCPT TO 或 DATA 阶段，已修正测试凭据。')
    command = [sys.executable, 'scripts/mail_delivery.py', 'resolve', '--config', str(config), '--invocation', invocation,
        '--decision', 'NOT_EXECUTED', '--expected-hash', row['request_hash'], '--evidence-file', str(evidence), '--expected-revision', str(row['revision'])]
    with delivery_lock(subject, invocation):
        conflict = subprocess.run(command, text=True, capture_output=True)
        assert conflict.returncode != 0 and '仍持有锁' in conflict.stderr
    changed = subprocess.run(command, text=True, capture_output=True, check=True)
    assert json.loads(changed.stdout)['status'] == 'RETRY_ALLOWED'
    repeated = subprocess.run(command, text=True, capture_output=True)
    assert repeated.returncode != 0
    sent = operation('send', call)
    assert sent['status'] == 'accepted_by_smtp' and not sent['deduplicated']
    assert operation('send', call)['deduplicated'] is True
    with pytest.raises(HTTPException):
        resolve_delivery(subject, invocation, 'NOT_EXECUTED', row['request_hash'], evidence.read_text(), row['revision'])
