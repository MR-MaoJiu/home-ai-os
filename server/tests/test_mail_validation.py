"""邮件输入和成员绑定边界；无效请求在连接邮箱前拒绝。"""
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from homeai_providers.mail_adapter import operation


def test_mail_subject_and_header_boundaries(monkeypatch):
    monkeypatch.setenv('MAIL_SUBJECT_ID', 'member-one')
    monkeypatch.setenv('MAIL_USER', 'local@example.test')
    with pytest.raises(HTTPException) as denied:
        operation('search', SimpleNamespace(subject_id='member-two', arguments={}, invocation_id='one'))
    assert denied.value.status_code == 403
    for arguments in [
        {'to': 'a@example.test,b@example.test', 'subject': '不能多收件人'},
        {'to': 'a@example.test', 'subject': '注入\r\nBcc: b@example.test'},
        {'to': 'a@example.test', 'subject': '附件', 'attachment': '/private/file'},
    ]:
        with pytest.raises(HTTPException) as invalid:
            operation('send', SimpleNamespace(subject_id='member-one', arguments=arguments, invocation_id='one'))
        assert invalid.value.status_code == 422
