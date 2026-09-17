"""真实 MCP 进程/HTTP 服务和文件读取，不替代协议返回值。"""
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest
from fastapi import HTTPException
from mcp import ClientSession
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from homeai_providers.mcp_adapter import parameters, invoke_stdio
from homeai_providers.mcp_contract import catalog, checked_call


def config(tmp_path):
    (tmp_path/'document.txt').write_text('真实 MCP 文件内容：家庭备份安排')
    executable=Path(sys.executable).absolute()
    server=Path('server/tests/fixtures/mcp_file_server.py').resolve()
    return {'command':str(executable),'command_sha256':hashlib.sha256(executable.read_bytes()).hexdigest(),
        'args':[str(server)],'file_sha256':{str(server):hashlib.sha256(server.read_bytes()).hexdigest()},
        'env':{'MCP_READ_ROOT':str(tmp_path)},'tools':{'read':'read_text'}}


def http_errors(exc):
    if isinstance(exc,BaseExceptionGroup):return [item for child in exc.exceptions for item in http_errors(child)]
    return [exc] if isinstance(exc,HTTPException) else []


@pytest.mark.asyncio
async def test_real_stdio_catalog_drift_and_file_integrity(tmp_path,monkeypatch):
    settings=config(tmp_path)
    async with stdio_client(parameters(settings)) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            fingerprint,tools=await catalog(session)
            assert {tool['name'] for tool in tools}=={'read_text','parse_utf8'}
    settings['catalog_sha256']=fingerprint
    path=tmp_path/'provider.json';path.write_text(json.dumps(settings));path.chmod(0o600)
    monkeypatch.setenv('MCP_CONFIG_FILE',str(path))
    value=await invoke_stdio('read',SimpleNamespace(arguments={'name':'document.txt'}))
    assert '真实 MCP 文件内容' in value['result']
    (tmp_path/'catalog-change').write_text('新增工具')
    with pytest.raises(Exception) as changed:
        await invoke_stdio('read',SimpleNamespace(arguments={'name':'document.txt'}))
    assert any(item.status_code==409 for item in http_errors(changed.value))
    settings['command_sha256']='0'*64
    with pytest.raises(HTTPException):parameters(settings)


@pytest.mark.asyncio
async def test_real_streamable_http_and_unmapped_tool(tmp_path):
    import socket
    settings=config(tmp_path)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    environment={**os.environ,'MCP_READ_ROOT':str(tmp_path),'MCP_TEST_PORT':str(port),'MCP_TEST_TRANSPORT':'streamable-http'}
    process=subprocess.Popen([settings['command'],*settings['args']],env=environment,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    url=f'http://127.0.0.1:{port}/mcp'
    try:
        async with httpx.AsyncClient(trust_env=False,timeout=2) as client:
            for _ in range(50):
                try:
                    await client.get(url);break
                except httpx.TransportError:
                    import asyncio
                    await asyncio.sleep(0.1)
            else:raise AssertionError('实际 HTTP MCP 服务未启动')
            async with streamable_http_client(url,http_client=client) as streams:
                async with ClientSession(streams[0],streams[1]) as session:
                    await session.initialize()
                    fingerprint,_=await catalog(session)
                    value=await checked_call(session,'read_text',{'name':'document.txt'},fingerprint)
                    assert '家庭备份安排' in value['content'][0]['text']
                    with pytest.raises(HTTPException) as denied:
                        await checked_call(session,'unknown',{},fingerprint)
                    assert denied.value.status_code==403
    finally:
        process.terminate()
        try:process.wait(timeout=10)
        except subprocess.TimeoutExpired:process.kill();process.wait()


from test_workflows import workflow

@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv('HOMEAI_INTEGRATION')!='1',reason='需要真实 PostgreSQL 与 OPA')
async def test_core_document_parse_through_real_mcp(workflow,tmp_path):
    import asyncio,socket,uuid
    from homeai.db import Provider
    from homeai.contracts import ProviderManifest
    from homeai.runtime import run_task
    from test_docling_live import upload
    app,user=workflow
    settings=config(tmp_path)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    environment={**os.environ,'MCP_READ_ROOT':str(tmp_path),'MCP_TEST_PORT':str(port),'MCP_TEST_TRANSPORT':'streamable-http'}
    process=subprocess.Popen([settings['command'],*settings['args']],env=environment,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    url=f'http://127.0.0.1:{port}/mcp'
    identifier='a.mcp.'+uuid.uuid4().hex
    try:
        async with httpx.AsyncClient(trust_env=False,timeout=2) as client:
            for _ in range(50):
                try:await client.get(url);break
                except httpx.TransportError:await asyncio.sleep(0.1)
            else:raise AssertionError('MCP 服务未启动')
            async with streamable_http_client(url,http_client=client) as streams:
                async with ClientSession(streams[0],streams[1]) as session:
                    await session.initialize();fingerprint,_=await catalog(session)
        manifest=ProviderManifest(id=identifier,version='utf8-1',adapter='mcp',endpoint=url,
            allowed_hosts=['127.0.0.1'],mcp_catalog_sha256=fingerprint,capabilities={'document.parse@v1':'parse_utf8'})
        with app.db() as db:
            db.add(Provider(id=identifier,manifest=manifest.model_dump_json(),enabled=True));db.commit()
        source=upload(user,'mcp.txt','真实 MCP 解析链路：周五检查备份。'.encode())
        response=user.request('POST','/api/v1/files/'+source+'/parse')
        assert response.status_code==202,response.text
        task_id=response.json()['id']
        await run_task(app,task_id,user.user_id)
        task=user.request('GET','/api/v1/tasks/'+task_id).json()
        assert task['status']=='SUCCEEDED',task
        assert '周五检查备份' in task['result']['markdown']
        record=user.request('GET','/api/v1/data/'+task['result']['record_id']).json()
        assert record['payload']['source_ids']==[source]
    finally:
        process.terminate()
        try:process.wait(timeout=10)
        except subprocess.TimeoutExpired:process.kill();process.wait()
        with app.db() as db:
            row=db.get(Provider,identifier)
            if row:row.enabled=False
            db.commit()
