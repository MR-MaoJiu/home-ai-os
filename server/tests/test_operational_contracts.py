"""独立进程验证模型登记、公开契约及实际加密心跳，不启动业务执行器。"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

from homeai.diagnostics import runtime_check
from homeai.heartbeat import Heartbeat


def test_cli_metadata_registration_without_api_or_database():
    code = "from homeai.schema import registered_metadata;import json;print(json.dumps(sorted(registered_metadata().tables)))"
    result = subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,check=True,
        env={**os.environ,'PYTHONPATH':str(Path('server').resolve())})
    tables=set(json.loads(result.stdout))
    assert {'media_uploads','media_chunks','media_jobs','model_configurations','model_verifications',
        'model_usage','redaction_artifacts','client_actions','task_data_grants','memory_learning',
        'memory_forget_tombstones','memory_forget_sources'}<=tables
    help_result=subprocess.run([sys.executable,'-m','homeai.cli','worker','--help'],capture_output=True,text=True,check=True,
        env={**os.environ,'PYTHONPATH':str(Path('server').resolve())})
    assert '--service' in help_result.stdout and 'media' in help_result.stdout and 'notification' in help_result.stdout


def test_public_schema_contains_rich_inputs_and_upload_contracts():
    path=Path('scripts/export_contracts.py')
    spec=importlib.util.spec_from_file_location('homeai_contract_export_test',path)
    exporter=importlib.util.module_from_spec(spec);spec.loader.exec_module(exporter)
    docs=exporter.documents()
    assert {'MediaUploadRequest.json','ConversationMessage.json','ClientContext.json','MessagePart.json',
        'ModelRoutingConfiguration.json','ClientActionResponse.json','ClientActionDenial.json'}<=docs.keys()
    upload=json.loads(docs['MediaUploadRequest.json'])
    assert upload['additionalProperties'] is False
    assert {'client_id','sha256','size','kind'}<=set(upload['required'])
    message=json.loads(docs['ConversationMessage.json'])
    assert 'parts' in message['properties'] and 'client_context' in message['properties']
    api=json.loads(docs['openapi.json'])
    for path in ('/api/v1/uploads','/api/v1/uploads/limits','/api/v1/manage/models','/api/v1/client-actions'):
        assert path in api['paths']
    assert 'private_key' not in json.loads(docs['ModelRoutingConfiguration.json'])['properties']


async def test_missing_media_worker_and_notification_worker_are_explicit(system):
    app=system[0].state
    for name in ('core-worker','memory-worker'):
        heartbeat=Heartbeat(app,name);heartbeat.progress();heartbeat.write()
    check,_=runtime_check(app)
    assert not check['ok']
    assert {'media-worker','notification-worker'}<=set(check['missing'])
    assert '附件解析' in check['reason'] and '通知投递' in check['reason']
    for name in ('media-worker','notification-worker'):
        heartbeat=Heartbeat(app,name);heartbeat.progress();heartbeat.write()
    check,workers=runtime_check(app)
    assert check['ok'],check
    assert workers['media-worker']['recent_heartbeat']
    assert 'backup-worker' not in check['required']


async def test_enabled_backup_requires_its_worker_but_default_does_not(system):
    from homeai.backup_settings import Store
    app=system[0].state
    for name in ('core-worker','memory-worker','media-worker','notification-worker'):
        heartbeat=Heartbeat(app,name);heartbeat.progress();heartbeat.write()
    store=Store(app);value=store.read()
    value['config'].update(enabled=True,directory=str(app.settings.state_dir/'selected-backups'),frequency='daily')
    store.write(value)
    check,_=runtime_check(app)
    assert check['missing']==['backup-worker']
    assert not (app.settings.state_dir/'selected-backups').exists()
