"""审批、成员隔离和真实宿主进程闭环；测试只操作各用例的临时目录。"""
import json
import uuid
import pytest
from sqlalchemy import select
from homeai.db import Task, Invocation, Device, Principal
from homeai.runtime import run_task
from homeai.security import Actor
from homeai.shell_tools import ShellRequest, prepare
from homeai.shell_configuration import ShellPolicy, HostAction
from homeai.tool_availability import available_tools
from conftest import SignedClient


def actor(alice):
    return Actor(alice.user_id, 'h1', alice.device_id, 'infrastructure_owner')


def configure(system, alice, script='printf actual-host-output', **extra):
    path = system[0].state.settings.state_dir / 'shell-policy.json'
    policy = ShellPolicy(host_enabled=True, host_members=[alice.user_id],
        host_actions=[HostAction(id='diagnose',title='临时测试动作',script=script,cwd=str(path.parent),timeout_seconds=2)], **extra)
    path.write_text(policy.model_dump_json()); path.chmod(0o600)
    return path


def submit(alice, capability='host.execute@v1', args=None):
    response = alice.request('POST','/api/v1/tasks',{'idempotency_key':str(uuid.uuid4()),
        'capability':capability,'arguments':args or {'action_id':'diagnose'}})
    assert response.status_code == 202, response.text
    return response.json()['id']


async def approve(system, alice, tid):
    await run_task(system[0].state, tid, alice.user_id)
    result = alice.request('GET','/api/v1/tasks/'+tid).json()
    assert result['status'] == 'AWAITING_APPROVAL', result
    approval = next(row for row in alice.request('GET','/api/v1/approvals').json() if row['task_id'] == tid)
    assert 'script' in approval['arguments']['execution']
    assert alice.request('POST','/api/v1/approvals/'+approval['id'],{'decision':'APPROVED'}).status_code == 200
    return approval


async def test_host_executes_only_after_approval_and_keeps_private_report(system,alice):
    target = system[0].state.settings.state_dir / 'actual-file.txt'
    configure(system,alice,'printf original > actual-file.txt; printf actual-host-output')
    tid = submit(alice)
    await approve(system,alice,tid)
    assert not target.exists()
    await run_task(system[0].state,tid,alice.user_id)
    assert target.read_text() == 'original'
    result = alice.request('GET','/api/v1/tasks/'+tid).json()
    assert result['status'] == 'SUCCEEDED', result
    assert result['result']['exit_code'] == 0
    assert result['result']['stdout'] == 'actual-host-output'
    part = result['result']['parts'][0]
    report = alice.request('GET','/api/v1/assets/'+part['record_id']+'/content')
    assert report.status_code == 200 and b'actual-host-output' in report.content
    bob = SignedClient(system[1],system[2])
    assert bob.request('GET','/api/v1/assets/'+part['record_id']+'/content').status_code == 404
    # 已完成步骤不会在worker再次扫描时执行第二次。
    target.write_text('do-not-replay')
    await run_task(system[0].state,tid,alice.user_id)
    assert target.read_text() == 'do-not-replay'


async def test_host_edit_and_delete_wait_for_consent_with_full_command_and_impact(system, alice):
    path = configure(system, alice, 'printf changed > keep.txt; rm remove.txt')
    original = path.parent / 'keep.txt'
    removed = path.parent / 'remove.txt'
    original.write_text('original'); removed.write_text('preserved')
    policy = json.loads(path.read_text())
    policy['host_actions'][0].update(impact_summary='更新 keep.txt 并删除 remove.txt',
        file_changes=['覆盖 keep.txt；删除 remove.txt'], service_changes=['无服务变更'])
    path.write_text(json.dumps(policy))
    tid = submit(alice)
    await run_task(system[0].state, tid, alice.user_id)
    approval = next(row for row in alice.request('GET', '/api/v1/approvals').json() if row['task_id'] == tid)
    context = approval['arguments']['approval_context']
    import shlex
    assert shlex.split(context['command']) == ['/bin/sh', '-c', 'printf changed > keep.txt; rm remove.txt']
    assert context['source'] == 'local_administrator'
    assert context['effects']['summary'] == '更新 keep.txt 并删除 remove.txt'
    assert context['effects']['file_changes'] == ['覆盖 keep.txt；删除 remove.txt']
    assert 'UID' in context['execution_identity'] and str(path.parent) in context['execution_location']
    assert original.read_text() == 'original' and removed.read_text() == 'preserved'
    assert alice.request('POST', '/api/v1/approvals/' + approval['id'], {'decision': 'APPROVED'}).status_code == 200
    await run_task(system[0].state, tid, alice.user_id)
    assert original.read_text() == 'changed' and not removed.exists()


async def test_denied_host_delete_keeps_original_file(system, alice):
    path = configure(system, alice, 'rm protected.txt')
    target = path.parent / 'protected.txt'; target.write_text('preserved')
    tid = submit(alice)
    await run_task(system[0].state, tid, alice.user_id)
    approval = next(row for row in alice.request('GET', '/api/v1/approvals').json() if row['task_id'] == tid)
    assert '尚未登记完整影响' in approval['arguments']['approval_context']['effects']['summary']
    assert alice.request('POST', '/api/v1/approvals/' + approval['id'], {'decision': 'REJECTED'}).status_code == 200
    await run_task(system[0].state, tid, alice.user_id)
    assert target.read_text() == 'preserved'


@pytest.mark.parametrize('change', [
    {'impact_summary': '改为新的影响说明'}, {'file_changes': ['修改另一文件']}, {'service_changes': ['重启另一服务']},
])
async def test_changed_registered_impact_invalidates_approved_command(system, alice, change):
    path = configure(system, alice, 'touch should-not-exist')
    tid = submit(alice); await approve(system, alice, tid)
    value = json.loads(path.read_text()); value['host_actions'][0].update(change)
    path.write_text(json.dumps(value))
    await run_task(system[0].state, tid, alice.user_id)
    assert alice.request('GET', '/api/v1/tasks/' + tid).json()['status'] == 'FAILED'
    assert not (path.parent / 'should-not-exist').exists()


def test_sandbox_execution_separates_model_claim_from_enforced_limits(system, alice):
    configure(system, alice, sandbox_enabled=True, sandbox_image='sha256:' + 'a' * 64, sandbox_members=[alice.user_id])
    with system[2]() as db:
        args = prepare(system[0].state, db, actor(alice), {}, 'shell.run@v1', {
            'language': 'python', 'script': "from pathlib import Path\nPath('file.txt').write_text('updated')",
            'impact_summary': '宣称只读取，不会产生任何修改', 'outputs': [{'path': 'file.txt'}],
        })
    context = args['approval_context']
    assert context['source'] == 'sandbox_policy'
    assert context['proposal_impact'] == '宣称只读取，不会产生任何修改'
    assert '删除' in context['effects']['file_changes'][0]
    import shlex
    assert shlex.split(context['command']) == ['python3', '-I', '-c', args['script']]
    assert context['resource_scope'] == ['返回：/workspace/output/file.txt']
    from homeai.policy import CAPABILITIES
    assert CAPABILITIES['shell.run@v1'][0] == 2


async def test_authorized_sandbox_continues_without_approval_and_does_not_replay(system, alice, monkeypatch):
    from homeai import shell_sandbox
    configure(system, alice, sandbox_enabled=True, sandbox_image='sha256:' + 'a' * 64, sandbox_members=[alice.user_id])
    calls=[]
    async def execute(**parameters):
        calls.append(parameters)
        assert parameters['inputs']=={} and parameters['outputs']==['result.txt']
        return {'exit_code':0,'stdout':'computed','stderr':'','truncated':False,'files':{'result.txt':b'42'}}
    monkeypatch.setattr(shell_sandbox,'run_sandbox',execute)
    tid=submit(alice,'shell.run@v1',{'language':'python','script':"open('result.txt','w').write(str(6*7))",'outputs':[{'path':'result.txt'}]})
    await run_task(system[0].state,tid,alice.user_id)
    result=alice.request('GET','/api/v1/tasks/'+tid).json()
    assert result['status']=='SUCCEEDED',result
    assert not any(row['task_id']==tid for row in alice.request('GET','/api/v1/approvals').json())
    output=next(part for part in result['result']['parts'] if part['title']=='result.txt')
    assert alice.request('GET','/api/v1/assets/'+output['record_id']+'/content').content==b'42'
    await run_task(system[0].state,tid,alice.user_id)
    assert len(calls)==1


async def test_automatic_sandbox_still_requires_local_member_authorization(system, alice, monkeypatch):
    async def fail(**parameters):raise AssertionError('未获本机成员授权不能执行')
    monkeypatch.setattr('homeai.shell_sandbox.run_sandbox',fail)
    configure(system, alice, sandbox_enabled=True, sandbox_image='sha256:' + 'a' * 64, sandbox_members=[])
    tid=submit(alice,'shell.run@v1',{'script':'printf safe'})
    await run_task(system[0].state,tid,alice.user_id)
    result=alice.request('GET','/api/v1/tasks/'+tid).json()
    assert result['status']=='FAILED' and '未获授权' in result['error']


async def test_approval_context_tampering_is_rejected_before_execution(system, alice):
    from homeai.shell_tools import execute
    from fastapi import HTTPException
    configure(system, alice, 'touch unapproved-context')
    with system[2]() as db:
        prepared = prepare(system[0].state, db, actor(alice), {}, 'host.execute@v1', {'action_id': 'diagnose'})
        prepared['approval_context']['effects']['summary'] = '伪造安全说明'
        with pytest.raises(HTTPException, match='重新提出任务并审批'):
            await execute(system[0].state, db, actor(alice), None, {}, 'host.execute@v1', prepared, 'unused')
    assert not (system[0].state.settings.state_dir / 'unapproved-context').exists()


async def test_changed_local_action_invalidates_old_approval(system,alice):
    path = configure(system,alice)
    tid = submit(alice); await approve(system,alice,tid)
    value = json.loads(path.read_text()); value['host_actions'][0]['script'] = 'touch unapproved'
    path.write_text(json.dumps(value))
    await run_task(system[0].state,tid,alice.user_id)
    result = alice.request('GET','/api/v1/tasks/'+tid).json()
    assert result['status'] == 'FAILED'
    assert '审批' in result['error']
    assert not (path.parent/'unapproved').exists()


async def test_host_nonzero_is_failed_and_cannot_blindly_retry(system,alice):
    configure(system,alice,'printf failure-detail >&2; exit 7')
    tid = submit(alice); await approve(system,alice,tid)
    await run_task(system[0].state,tid,alice.user_id)
    result = alice.request('GET','/api/v1/tasks/'+tid).json()
    assert result['status'] == 'FAILED' and result['result']['exit_code'] == 7
    assert result['result']['stderr'] == 'failure-detail'
    assert not alice.request('GET',f'/api/v1/tasks/{tid}/retry').json()['can_retry']


async def test_host_timeout_needs_reconciliation(system,alice):
    configure(system,alice,'sleep 20')
    tid = submit(alice); await approve(system,alice,tid)
    await run_task(system[0].state,tid,alice.user_id)
    result = alice.request('GET','/api/v1/tasks/'+tid).json()
    assert result['status'] == 'NEEDS_RECONCILIATION', result
    assert not alice.request('GET',f'/api/v1/tasks/{tid}/retry').json()['can_retry']


@pytest.mark.parametrize('role,household',[('adult','h1'),('infrastructure_owner','h2')])
async def test_other_member_cannot_execute_host_or_discover_scripts(system,alice,role,household):
    configure(system,alice)
    other = SignedClient(system[1],system[2],household=household,role=role)
    tid = submit(other)
    await run_task(system[0].state,tid,other.user_id)
    assert other.request('GET','/api/v1/tasks/'+tid).json()['status'] == 'FAILED'
    with system[2]() as db:
        tools,_ = available_tools(system[0].state,db,Actor(other.user_id,household,other.device_id,role))
    assert 'execute_host_action' not in [item['function']['name'] for item in tools]
    if role == 'adult':
        assert other.request('GET','/api/v1/manage/shell').status_code == 403
    else:
        assert other.request('GET','/api/v1/manage/shell').json()['host']['actions'] == []


@pytest.mark.parametrize('flag',[{'_automation_scope':'family'},{'_automation_id':'timer'}])
async def test_host_automation_cannot_publish_admin_outputs(system,alice,flag):
    configure(system,alice)
    tid = submit(alice)
    with system[2]() as db:
        task = db.get(Task,tid); body = system[0].state.vault.open(task.request,alice.user_id+':task:'+tid)
        body.update(flag); task.request = system[0].state.vault.seal(body,alice.user_id+':task:'+tid); db.commit()
    await run_task(system[0].state,tid,alice.user_id)
    assert alice.request('GET','/api/v1/tasks/'+tid).json()['status'] == 'FAILED'


async def test_host_device_revocation_after_approval_blocks_execution(system,alice):
    path=configure(system,alice,'touch should-not-exist')
    tid = submit(alice); await approve(system,alice,tid)
    with system[2]() as db:
        db.get(Device,alice.device_id).revoked=True; db.commit()
    await run_task(system[0].state,tid,alice.user_id)
    assert not (path.parent/'should-not-exist').exists()
    with system[2]() as db:
        assert db.get(Task,tid).status == 'FAILED'


@pytest.mark.parametrize('args',[{'action_id':'diagnose','script':'id'},{'action_id':'diagnose','cwd':'/'},{'action_id':'unknown'}])
async def test_model_cannot_supply_commands_or_unregistered_host_action(system,alice,args):
    configure(system,alice)
    tid=submit(alice,args=args); await run_task(system[0].state,tid,alice.user_id)
    assert alice.request('GET','/api/v1/tasks/'+tid).json()['status']=='FAILED'


@pytest.mark.parametrize('path',['/etc/passwd','../escape','a/../b','a//b','a\\b','a\x00b'])
def test_shell_artifact_paths_cannot_escape(path):
    with pytest.raises(ValueError):
        ShellRequest(script='true',outputs=[{'path':path}])


def test_production_sandbox_without_runsc_is_blocked(system,alice):
    configure(system,alice,sandbox_enabled=True,sandbox_image='sha256:'+'a'*64,sandbox_members=[alice.user_id])
    system[0].state.settings.environment='production'
    with system[2]() as db:
        with pytest.raises(Exception,match='runsc'):
            prepare(system[0].state,db,actor(alice),{},'shell.run@v1',{'script':'id'})


def test_shell_has_no_web_write_endpoint(system,alice):
    assert alice.request('PUT','/api/v1/manage/shell',{'host_enabled':True}).status_code == 405


@pytest.mark.skipif(not __import__('os').environ.get('HOMEAI_SHELL_TEST_IMAGE'), reason='需要本机固定镜像进行真实 Docker 验收')
@pytest.mark.parametrize('language',['shell','python'])
async def test_real_docker_automatic_inputs_and_encrypted_artifact_roundtrip(system,alice,language):
    from test_media import upload
    image=__import__('os').environ['HOMEAI_SHELL_TEST_IMAGE']
    configure(system,alice,sandbox_enabled=True,sandbox_image=image,sandbox_members=[alice.user_id])
    record=upload(alice,b'2\n3\n5\n','numbers.txt','file').json()['asset']
    script="python3 -I - <<'SCRIPT'\nfrom pathlib import Path\nvalues=[int(x) for x in Path('/workspace/input/numbers.txt').read_text().split()]\nPath('/workspace/output/result.txt').write_text(str(sum(values)))\nprint('computed')\nSCRIPT"
    if language == 'python':
        script=script.split("\n",1)[1].rsplit("\nSCRIPT",1)[0]
    tid=submit(alice,'shell.run@v1',{'language':language,'script':script,'inputs':[{'record_id':record['record_id'],'filename':'numbers.txt'}],
        'outputs':[{'path':'result.txt','kind':'file'}]})
    await run_task(system[0].state,tid,alice.user_id)
    assert not any(row['task_id']==tid for row in alice.request('GET','/api/v1/approvals').json())
    with system[2]() as db:
        invocation=db.scalar(select(Invocation).where(Invocation.task_id==tid))
        arguments=system[0].state.vault.open(invocation.arguments,alice.user_id+':invocation:'+invocation.id)
        assert arguments['script']==script
        assert arguments['execution']['source_versions']=={record['record_id']:1}
    result=alice.request('GET','/api/v1/tasks/'+tid).json()
    assert result['status']=='SUCCEEDED',result
    output=next(part for part in result['result']['parts'] if part['title']=='result.txt')
    assert output['provenance']['origin']=='shell_created'
    assert alice.request('GET','/api/v1/assets/'+output['record_id']+'/content').content==b'10'
    assert alice.request('GET','/api/v1/assets/'+record['record_id']+'/content').content==b'2\n3\n5\n'
    bob=SignedClient(system[1],system[2])
    assert bob.request('GET','/api/v1/assets/'+output['record_id']+'/content').status_code==404
    assert alice.request('DELETE','/api/v1/data/'+record['record_id']).status_code==200
    assert alice.request('GET','/api/v1/tasks/'+tid).json()['result'] is None
    assert alice.request('GET','/api/v1/assets/'+output['record_id']+'/content').status_code==404


@pytest.mark.parametrize('path',[[],['stdout'],['stderr'],['parts'],['parts',0,'record_id']])
def test_host_output_cannot_be_forwarded_by_step_references(system,alice,path):
    from homeai.runtime import resolve_arguments
    row=Invocation(id='host-step',capability='host.execute@v1',result=system[0].state.vault.seal(
        {'status':'completed','exit_code':0,'stdout':'private-machine-output','stderr':'','parts':[]},alice.user_id+':invocation-result:host-step'))
    with pytest.raises(Exception,match='私有执行报告'):
        resolve_arguments({'$step':0,'path':path},{0:row},system[0].state.vault,alice.user_id)
    assert resolve_arguments({'$step':0,'path':['exit_code']},{0:row},system[0].state.vault,alice.user_id)==0
