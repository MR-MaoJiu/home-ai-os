"""健康时间错误按实际原因分类，不读取系统或个人健康数据。"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from homeai import client_actions
from homeai.client_actions import HealthRequestValidationError, parameters_for, validate_planned_health_request


CURRENT = datetime(2026, 10, 2, 13, 32, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(client_actions, 'now', lambda: CURRENT.timestamp())


def values():
    return {'types': ['steps'], 'start_at': (CURRENT - timedelta(days=7)).isoformat(), 'end_at': CURRENT.isoformat()}


@pytest.mark.parametrize('change,code', [
    ({'end_at': None}, 'health_time_missing'),
    ({'end_at': ''}, 'health_time_missing'),
    ({'end_at': 'now'}, 'health_time_invalid_format'),
    ({'end_at': '私人输入不是日期'}, 'health_time_invalid_format'),
    ({'end_at': float('inf')}, 'health_time_invalid_format'),
    ({'end_at': True}, 'health_time_invalid_format'),
    ({'end_at': '2026-10-02T21:32:00'}, 'health_time_missing_timezone'),
    ({'end_at': '2026-10-02'}, 'health_time_missing_timezone'),
    ({'end_at': '2026-10-02T23:59:59+08:00'}, 'health_time_future_end'),
    ({'start_at': CURRENT.isoformat()}, 'health_time_invalid_order'),
    ({'start_at': (CURRENT + timedelta(hours=1)).isoformat()}, 'health_time_invalid_order'),
    ({'start_at': (CURRENT - timedelta(days=31, seconds=1)).isoformat()}, 'health_time_range_exceeded'),
])
def test_specific_time_failure(change, code):
    with pytest.raises(HealthRequestValidationError) as raised:
        parameters_for('read_health', values() | change)
    assert raised.value.status_code == 422 and raised.value.code == code
    assert '私人输入不是日期' not in raised.value.detail


def test_missing_start_is_specific():
    parameters = values()
    del parameters['start_at']
    with pytest.raises(HealthRequestValidationError) as raised:
        parameters_for('read_health', parameters)
    assert raised.value.code == 'health_time_missing'


@pytest.mark.parametrize('ending', ['2026-10-02T13:32:00Z', '2026-10-02T21:32:00+08:00'])
def test_equivalent_zones_and_exact_limit_are_preserved(ending):
    parameters = values() | {'start_at': (CURRENT - timedelta(days=31)).isoformat(), 'end_at': ending}
    assert parameters_for('read_health', parameters) == parameters


def test_clock_skew_allowance_has_a_boundary():
    parameters = values() | {'end_at': (CURRENT + timedelta(seconds=300)).isoformat()}
    assert parameters_for('read_health', parameters) == parameters
    parameters['end_at'] = (CURRENT + timedelta(seconds=301)).isoformat()
    with pytest.raises(HealthRequestValidationError) as raised:
        parameters_for('read_health', parameters)
    assert raised.value.code == 'health_time_future_end'


def test_preflight_only_validates_health_and_does_not_swallow_generic_error():
    assert validate_planned_health_request({'kind': 'provide_text'}) is None
    assert validate_planned_health_request({'kind': 'read_health', 'parameters': values()}) is None
    with pytest.raises(HTTPException) as raised:
        validate_planned_health_request({'kind': 'read_health', 'parameters': values() | {'override': True}})
    assert not isinstance(raised.value, HealthRequestValidationError)
