"""固定版本 MLX HTTP 适配；不允许请求动态加载模型、适配器或草稿模型。"""
import io
import json
import sys
from importlib.metadata import version
from pathlib import Path

if version('mlx-lm') != '0.31.3':
    raise SystemExit('MLX SDK 版本与已验证版本不符')
from mlx_lm import server

model = str(Path(__file__).resolve().parents[1] / 'state/models/mlx-qwen3-4b')
original_post = server.APIHandler.do_POST
original_load = server.ModelProvider.load


def fixed_load(self, model_path, adapter_path=None, draft_model_path=None):
    if model_path not in ('default_model', model) or adapter_path is not None or draft_model_path not in (None, 'default_model'):
        raise ValueError('只允许固定本地模型')
    return original_load(self, 'default_model', None, 'default_model')


def guarded_post(self):
    try:
        size = int(self.headers.get('Content-Length', '0'))
        if not 0 < size <= 65536:
            raise ValueError('请求大小无效')
        raw = self.rfile.read(size)
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError('请求格式无效')
        if body.get('model', 'default_model') not in ('default_model', model):
            raise ValueError('模型未授权')
        if body.get('adapters') is not None or body.get('draft_model', 'default_model') not in (None, 'default_model'):
            raise ValueError('不允许动态模型')
        limit = body.get('max_completion_tokens')
        if limit is None:
            limit = body.get('max_tokens', 512)
        if type(limit) is not int or not 1 <= limit <= 4096:
            raise ValueError('输出上限无效')
    except (ValueError, TypeError, UnicodeError):
        self.send_response(400)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"error":"request rejected"}')
        return
    stream = self.rfile
    try:
        self.rfile = io.BytesIO(raw)
        original_post(self)
    finally:
        self.rfile = stream


server.ModelProvider.load = fixed_load
server.APIHandler.do_POST = guarded_post
server.APIHandler.log_message = lambda *args: None
sys.argv = ['mlx-server', '--model', model, '--host', '127.0.0.1', '--port', '58086',
            '--log-level', 'CRITICAL', '--allowed-origins', 'http://localhost',
            '--chat-template-args', '{"enable_thinking":false}',
            '--decode-concurrency', '1', '--prompt-concurrency', '1',
            '--prompt-cache-size', '1', '--max-tokens', '512']
server.main()
