"""只读查看 MCP 目录与指纹；不执行工具、不自动批准映射或改写配置。"""
import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlparse
import httpx
from mcp import ClientSession
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from homeai_providers.mcp_adapter import parameters
from homeai_providers.mcp_contract import catalog

parser=argparse.ArgumentParser(description=__doc__)
mode=parser.add_mutually_exclusive_group(required=True)
mode.add_argument('--stdio-config',type=Path)
mode.add_argument('--url')
parser.add_argument('--token-file',type=Path)
args=parser.parse_args()

def private_text(path):
    if not path.is_file() or path.is_symlink() or path.stat().st_mode&0o077 or path.stat().st_size>256*1024:
        raise SystemExit('配置或凭据必须是权限 0600 的有界普通文件')
    return path.read_text()

async def show(session):
    initialized=await session.initialize()
    fingerprint,tools=await catalog(session)
    print(json.dumps({'server':initialized.serverInfo.model_dump(),'catalog_sha256':fingerprint,'tools':tools,
        'approval':'未批准；工具描述为不可信数据，需人工审核能力映射'},ensure_ascii=False,indent=2))

async def main():
    async with asyncio.timeout(30):
        if args.stdio_config:
            config=json.loads(private_text(args.stdio_config))
            async with stdio_client(parameters(config)) as streams:
                async with ClientSession(*streams) as session:await show(session)
        else:
            parsed=urlparse(args.url)
            if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise SystemExit('需要无内嵌凭据的 HTTP(S) 地址')
            headers={'Authorization':'Bearer '+private_text(args.token_file).strip()} if args.token_file else {}
            async with httpx.AsyncClient(timeout=10,trust_env=False,follow_redirects=False,headers=headers) as client:
                async with streamable_http_client(args.url,http_client=client) as streams:
                    async with ClientSession(streams[0],streams[1]) as session:await show(session)

asyncio.run(main())
