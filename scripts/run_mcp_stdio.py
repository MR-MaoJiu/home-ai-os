"""启动固定配置的 MCP stdio 桥，不向子服务继承 Core 凭据。"""
import argparse
import json
import os
import sys
from pathlib import Path
from homeai_providers.mcp_adapter import parameters

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--config',required=True,type=Path)
parser.add_argument('--token-file',required=True,type=Path)
parser.add_argument('--port',type=int,default=8108)
args=parser.parse_args()
for path in (args.config,args.token_file):
    if not path.is_file() or path.is_symlink() or path.stat().st_mode&0o077 or path.stat().st_size>256*1024:
        raise SystemExit('配置与令牌必须为权限 0600 的有界普通文件')
config=json.loads(args.config.read_text())
parameters(config)
if len(config.get('catalog_sha256',''))!=64:
    raise SystemExit('先审核工具目录并配置 catalog_sha256')
token=args.token_file.read_text().strip()
if len(token)<32 or not 1024<=args.port<=65535:
    raise SystemExit('令牌或监听端口无效')
root=Path(__file__).resolve().parents[1]
environment={key:os.environ[key] for key in ('PATH','HOME','TMPDIR','LANG') if key in os.environ}
environment.update(HOMEAI_ADAPTER='mcp_stdio',MCP_CONFIG_FILE=str(args.config.resolve()),PROVIDER_SERVICE_TOKEN=token,PYTHONPATH=str(root/'providers'))
os.execve(sys.executable,[sys.executable,'-m','uvicorn','homeai_providers.service:app','--host','127.0.0.1','--port',str(args.port),'--no-access-log'],environment)
