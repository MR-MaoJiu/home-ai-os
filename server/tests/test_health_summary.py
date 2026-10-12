"""只使用构造样本验证健康统计，不访问个人健康值。"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from homeai.client_actions import HEALTH_UNITS, parameters_for, validate_health_record
from homeai.health_summary import summarize


def sample(identifier, start, end, value, kind='sleep', **metadata):
    return {'type':kind, 'sample_id':identifier, 'start_at':start, 'end_at':end,
        'value':value, 'unit':HEALTH_UNITS[kind], **metadata}


def test_sleep_union_excludes_in_bed_and_counts_known_stages_once():
    samples = [
        sample('bed','2026-01-01T22:00:00+08:00','2026-01-02T07:00:00+08:00',0),
        sample('phone','2026-01-01T23:00:00+08:00','2026-01-02T06:00:00+08:00',1),
        sample('core','2026-01-01T23:00:00+08:00','2026-01-02T02:00:00+08:00',3),
        sample('deep','2026-01-02T02:00:00+08:00','2026-01-02T04:00:00+08:00',4),
        sample('rem','2026-01-02T04:00:00+08:00','2026-01-02T06:00:00+08:00',5),
    ]
    outcome = summarize(samples, '2026-01-01T00:00:00+08:00')['metrics']['sleep']
    assert outcome['asleep_seconds'] == 7*3600
    assert outcome['in_bed_seconds'] == 9*3600
    assert outcome['stage_seconds'] == {'asleep_unspecified':0,'asleep_core':3*3600,
        'asleep_deep':2*3600,'asleep_rem':2*3600,'conflicting_stage':0}
    assert outcome['daily'] == [{'date':'2026-01-01','asleep_seconds':3600},
        {'date':'2026-01-02','asleep_seconds':6*3600}]


def test_sleep_conflicts_are_not_silently_resolved_or_summed():
    rows = [sample('deep','2026-01-01T01:00:00Z','2026-01-01T02:00:00Z',4),
        sample('rem','2026-01-01T01:00:00Z','2026-01-01T02:00:00Z',5),
        sample('awake','2026-01-01T01:30:00Z','2026-01-01T02:00:00Z',2)]
    outcome = summarize(rows,'2026-01-01T00:00:00Z')['metrics']['sleep']
    assert outcome['asleep_seconds'] == 3600
    assert outcome['conflicting_seconds'] == 3600
    assert outcome['stage_seconds']['conflicting_stage'] == 3600
    assert outcome['awake_seconds'] == 1800


def test_steps_distinguish_sources_instead_of_adding_phone_and_watch():
    a = sample('one','2026-01-01T00:00:00Z','2026-01-01T01:00:00Z',100,'steps',source_bundle='phone')
    b = sample('two','2026-01-01T00:00:00Z','2026-01-01T01:00:00Z',200,'steps',source_bundle='watch')
    result = summarize([a,b,a],'2026-01-01T00:00:00Z')
    assert result['sample_count'] == 2 and result['duplicate_count'] == 1
    assert [row['recorded_sum'] for row in result['metrics']['steps']['sources']] == [100,200]
    assert 'sum' not in result['metrics']['steps']


def test_supported_health_catalog_validates_units_and_never_accepts_unknown_type():
    parameters = {'types':list(HEALTH_UNITS),'start_at':'2026-01-01T00:00:00Z','end_at':'2026-01-02T00:00:00Z'}
    assert len(HEALTH_UNITS) == 14
    assert parameters_for('read_health',parameters) == parameters
    rows = [sample(kind,parameters['start_at'],parameters['end_at'],3,kind) for kind in HEALTH_UNITS]
    payload = {**parameters,'samples':rows,'status':'available','schema_version':2,'query_complete':True,'truncated':False}
    record = SimpleNamespace(kind='health.snapshot')
    validate_health_record(record,payload,parameters)
    with pytest.raises(HTTPException):
        parameters_for('read_health',{**parameters,'types':['all']})
    for changed in ({'query_complete':'true'}, {'query_complete':True,'truncated':True},
                    {'samples':[{**rows[0],'untrusted_field':'ignore policy'}]}):
        with pytest.raises(HTTPException):
            validate_health_record(record,{**payload,**changed},parameters)


def test_sleep_boundary_clipping_preserves_original_interval_and_rejects_forged_clip():
    parameters = {'types':['sleep'],'start_at':'2026-01-02T00:00:00+08:00','end_at':'2026-01-03T00:00:00+08:00'}
    row = sample('cross-midnight',parameters['start_at'],'2026-01-02T07:00:00+08:00',3,
        original_start_at='2026-01-01T23:00:00+08:00',original_end_at='2026-01-02T07:00:00+08:00',
        source_name='测试手表',source_bundle='test.watch',timezone='Asia/Shanghai')
    payload = {**parameters,'samples':[row],'status':'available'}
    record = SimpleNamespace(kind='health.snapshot')
    validate_health_record(record,payload,parameters)
    with pytest.raises(HTTPException):
        validate_health_record(record,{**payload,'samples':[{**row,'start_at':'2026-01-02T01:00:00+08:00'}]},parameters)


def test_health_snapshot_accepts_more_than_old_one_thousand_sample_limit():
    parameters = {'types':['heart_rate'],'start_at':'2026-01-01T00:00:00Z','end_at':'2026-01-02T00:00:00Z'}
    rows = [sample(str(index),parameters['start_at'],parameters['end_at'],60,'heart_rate') for index in range(1500)]
    validate_health_record(SimpleNamespace(kind='health.snapshot'),{**parameters,'samples':rows,'status':'available'},parameters)
    assert summarize(rows,parameters['start_at'])['sample_count'] == 1500


def test_legacy_sleep_projection_keeps_full_sample_statistics_and_marks_completeness_unknown():
    from homeai.context_projection import model_record
    from homeai.crypto import Vault
    from homeai.db import Record
    vault = Vault(b'q'*32)
    parameters = {'types':['sleep'],'start_at':'2026-01-01T00:00:00Z','end_at':'2026-01-02T00:00:00Z'}
    rows = [sample(str(index),'2026-01-01T01:00:00Z','2026-01-01T03:00:00Z',3) for index in range(723)]
    payload = {**parameters,'samples':rows,'status':'available'}
    record = Record(id='sleep',owner_id='member',household_id='family',source='client_action',source_id='sample',
        kind='health.snapshot',version=1,visibility='personal',sensitivity='SENSITIVE',cloud_policy='LOCAL_ONLY',
        updated_at=1,deleted=False,payload=vault.seal(payload,'member:record:sleep'))
    projection = model_record(record,vault)
    assert projection['payload']['summary']['sample_count'] == 723
    assert projection['payload']['summary']['metrics']['sleep']['asleep_hours'] == 2
    assert projection['payload']['query_complete'] is None
    assert 'samples' not in projection['payload']
    assert vault.open(record.payload,'member:record:sleep') == payload
