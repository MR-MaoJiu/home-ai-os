"""只初始化本机独立协议验收实例；凭据写入私有文件，不输出到终端。"""
import json
import os
import secrets
from pathlib import Path
import httpx

base = 'http://127.0.0.1:58123'
path = Path('state/homeassistant-test-auth.json')
with httpx.Client(timeout=20, trust_env=False) as client:
    if path.exists():
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise SystemExit('测试凭据必须为 0600 普通文件')
        saved = json.loads(path.read_text())
        response = client.post(base + '/auth/token', data={'grant_type': 'refresh_token', 'refresh_token': saved['refresh_token'], 'client_id': base + '/'})
        response.raise_for_status()
        saved.update(response.json())
    else:
        status = client.get(base + '/api/onboarding'); status.raise_for_status()
        if any(step['step'] == 'user' and step['done'] for step in status.json()):
            raise SystemExit('实例已有账户，禁止替换；请使用原有私有凭据')
        password = secrets.token_urlsafe(32)
        response = client.post(base + '/api/onboarding/users', json={'name': 'Home AI Protocol Verification', 'username': 'homeai-protocol', 'password': password, 'client_id': base + '/', 'language': 'en'})
        response.raise_for_status()
        code = response.json()['auth_code']
        response = client.post(base + '/auth/token', data={'grant_type': 'authorization_code', 'code': code, 'client_id': base + '/'})
        response.raise_for_status()
        saved = {**response.json(), 'username': 'homeai-protocol', 'password': password}
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as file: json.dump(saved, file)
    os.replace(temporary, path)
    response = client.get(base + '/api/states/switch.home_ai_protocol_switch', headers={'Authorization': 'Bearer ' + saved['access_token']})
    response.raise_for_status()
    print('测试账户认证通过，实际辅助实体状态：' + response.json()['state'])
