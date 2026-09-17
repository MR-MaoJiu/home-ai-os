import json
import os
from pathlib import Path
from fastapi import HTTPException


async def invoke_stdio(operation, call):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    # 进程与工具映射仅来自部署配置，调用者不能提供命令行、环境变量或工具名。
    config = json.loads(Path(os.environ['MCP_CONFIG_FILE']).read_text())
    tool = config['tools'].get(operation)
    if not tool:
        raise HTTPException(403, '未映射的 MCP 工具')
    command = Path(config['command'])
    if not command.is_absolute() or not command.is_file():
        raise RuntimeError('MCP 可执行文件必须是固定绝对路径')
    parameters = StdioServerParameters(command=str(command), args=config.get('args', []), env=config.get('env', {}))
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, call.arguments)
            if result.isError:
                raise HTTPException(502, 'MCP 工具执行失败')
            return result.model_dump(mode='json')
