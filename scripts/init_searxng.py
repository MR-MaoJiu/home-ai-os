"""初始化独立 SearXNG 私有配置，不覆盖现有文件或打印秘密。"""
import os
import secrets
from pathlib import Path
import yaml

root = Path(__file__).resolve().parents[1]
directory = root / 'state' / 'searxng'
directory.mkdir(parents=True, exist_ok=True)
path = directory / 'settings.yml'
settings = {
    'use_default_settings': {'engines': {'keep_only': ['duckduckgo', 'brave', 'wikipedia']}},
    'general': {'debug': False, 'instance_name': 'Home AI Search'},
    'server': {'secret_key': secrets.token_urlsafe(48), 'limiter': False, 'public_instance': False, 'image_proxy': False},
    'search': {'formats': ['json'], 'safe_search': 2, 'autocomplete': ''},
    'outgoing': {'request_timeout': 8.0, 'max_request_timeout': 12.0},
    'engines': [{'name': name, 'disabled': False} for name in ['duckduckgo', 'brave', 'wikipedia']],
}
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    print('保留现有 SearXNG 私有配置')
else:
    with os.fdopen(fd, 'w') as file:
        yaml.safe_dump(settings, file, allow_unicode=True)
    print('已创建 SearXNG 私有配置；未启用公网监听')
