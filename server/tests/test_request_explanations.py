"""校验提示只描述本次命中的问题，不拼接无关原因或回显敏感值。"""
import pytest
from fastapi import HTTPException
from homeai import host_inspection_tools
from homeai.privacy import validate_search


@pytest.mark.parametrize('query,expected', [
    ('   ', '搜索词不能为空'),
    ('x' * 501, '搜索词超过 500 个字符，请缩短后重试'),
    ('public\nquery!', '搜索词包含控制字符，请移除后重试'),
    ('!public', '搜索词包含不允许的 ! 控制语法，请移除后重试'),
])
def test_search_reports_only_first_actual_validation_failure(query, expected):
    with pytest.raises(HTTPException) as error:
        validate_search({'query': query})
    assert error.value.status_code == 422
    assert error.value.detail == expected


@pytest.mark.parametrize('query,label', [
    ('public@example.com', '邮箱地址'),
    ('13800138000', '手机号码'),
    ('110101199001010010', '身份证号码'),
    ('6222020000000000', '银行卡号码'),
])
def test_search_reports_detected_personal_information_kind_without_value(query, label):
    with pytest.raises(HTTPException) as error:
        validate_search({'query': query})
    assert error.value.status_code == 403
    assert error.value.detail == '搜索词包含已识别的' + label + '，不能发送到搜索引擎'
    assert query not in error.value.detail


def test_public_search_remains_allowed():
    assert validate_search({'query': '  Python 文档  '}) == {'query': 'Python 文档'}


@pytest.mark.parametrize('targets,allowed,expected', [
    (['cpu'], [], '当前成员或服务器未获CPU使用情况的读取权限'),
    (['memory'], [], '当前成员或服务器未获内存统计的读取权限'),
    (['cpu', 'memory'], ['cpu'], '当前成员或服务器未获内存统计的读取权限'),
    (['cpu', 'screenshot'], ['cpu'], '当前成员或服务器未获主显示器截屏的读取权限；截屏需本机管理员明确授权此成员'),
])
def test_host_inspection_reports_only_denied_requested_targets(monkeypatch, targets, allowed, expected):
    monkeypatch.setattr(host_inspection_tools, 'allowed_targets', lambda app, actor: allowed)
    with pytest.raises(HTTPException) as error:
        host_inspection_tools.prepare(None, None, None, {}, {'targets': targets, 'purpose': '本次公开测试'})
    assert error.value.status_code == 403
    assert error.value.detail == expected
