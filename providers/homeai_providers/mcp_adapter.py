"""固定启动配置的 stdio MCP 桥；调用者不能提交命令或环境。"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
from fastapi import HTTPException
from .mcp_contract import checked_call, protocol_errors


def file_digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def parameters(config):
    from mcp import StdioServerParameters
    command = Path(config['command'])
    if not command.is_absolute() or not command.is_file() or command.name in {'sh', 'bash', 'zsh', 'fish', 'npx', 'uvx'}:
        raise HTTPException(403, 'MCP 必须使用预先安装的固定可执行文件')
    if file_digest(command) != config.get('command_sha256'):
        raise HTTPException(409, 'MCP 可执行文件摘要已变化')
    arguments = config.get('args', [])
    if not isinstance(arguments, list) or len(arguments) > 32 or any(not isinstance(value, str) or len(value) > 4096 or value in {'-c', '-e', '--eval'} for value in arguments):
        raise HTTPException(422, '不接受内联代码或无效启动参数')
    cwd = Path(config['cwd']) if config.get('cwd') else Path.cwd()
    if not cwd.is_absolute() or not cwd.is_dir():
        raise HTTPException(422, 'MCP 工作目录必须为固定绝对路径')
    files = config.get('file_sha256', {})
    for name, expected in files.items():
        path = Path(name)
        if not path.is_absolute() or not path.is_file() or file_digest(path) != expected:
            raise HTTPException(409, 'MCP 启动文件摘要已变化')
    for argument in arguments:
        path = Path(argument)
        resolved = path if path.is_absolute() else cwd / path
        if resolved.is_file() and (not path.is_absolute() or str(path) not in files):
            raise HTTPException(403, 'MCP 启动文件必须为已审核的绝对路径')
    environment = config.get('env', {})
    if not isinstance(environment, dict) or any(not isinstance(key, str) or not isinstance(value, str) or key.startswith(('HOMEAI_', 'DYLD_', 'LD_')) or key in {'PROVIDER_SERVICE_TOKEN', 'MCP_CONFIG_FILE', 'PYTHONPATH', 'PYTHONHOME'} for key, value in environment.items()):
        raise HTTPException(403, 'MCP 子进程不能获得 Core 或桥接凭据')
    return StdioServerParameters(command=str(command), args=arguments, env=environment, cwd=config.get('cwd'))


def load_config():
    path = Path(os.environ['MCP_CONFIG_FILE'])
    if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077 or path.stat().st_size > 256 * 1024:
        raise HTTPException(503, 'MCP 配置必须是权限 0600 的有界普通文件')
    return json.loads(path.read_text())


async def invoke_stdio(operation, call):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
    config = load_config()
    tool = config['tools'].get(operation)
    if not tool:
        raise HTTPException(403, '未映射的 MCP 工具')
    async with protocol_errors(), asyncio.timeout(60):
        async with stdio_client(parameters(config)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await checked_call(session, tool, call.arguments, config.get('catalog_sha256'), structured=True)
