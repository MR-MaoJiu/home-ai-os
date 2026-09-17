"""真实 MCP 文件读取测试服务；仅允许读取显式测试目录中的文件。"""
import os
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

server = FastMCP('HomeAI protocol file reader', host='127.0.0.1', port=int(os.environ.get('MCP_TEST_PORT', '8119')), stateless_http=True)
root = Path(os.environ['MCP_READ_ROOT']).resolve()

@server.tool()
def read_text(name: str) -> str:
    """读取已授权目录中的文本文件。"""
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 10000:
        raise ValueError('文件不在允许范围')
    return path.read_text()

class ParsedText(BaseModel):
    markdown: str
    format: str
    parser_version: str

@server.tool()
def parse_utf8(filename: str, content_base64: str) -> ParsedText:
    """实际解码 UTF-8 文本附件，不处理其他格式。"""
    import base64
    if Path(filename).suffix != '.txt':
        raise ValueError('仅支持文本')
    content = base64.b64decode(content_base64, validate=True)
    if len(content) > 10000:
        raise ValueError('文本过大')
    return ParsedText(markdown=content.decode('utf-8'), format='txt', parser_version='utf8-1')

if (root / 'catalog-change').exists():
    @server.tool()
    def extra_read() -> str:
        """新版本增加的工具，必须触发目录变化拒绝。"""
        return (root / 'document.txt').read_text()

if __name__ == '__main__':
    server.run(transport=os.environ.get('MCP_TEST_TRANSPORT', 'stdio'))
