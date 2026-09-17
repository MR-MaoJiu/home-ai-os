"""验证真实发布镜像的安装路径、非 root 启动、管理资源和匿名鉴权边界。"""
import argparse
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--image', default='homeai-core:packaging-check')
args = parser.parse_args()
name = 'homeai-package-' + uuid.uuid4().hex[:12]
volume = name + '-state'


def docker(*arguments):
    return subprocess.check_output(['docker', *arguments], text=True).strip()


def read(base, path, expected=200):
    try:
        with urllib.request.urlopen(base + path, timeout=3) as response:
            status, body = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, body = error.code, error.read()
    if status != expected:
        raise RuntimeError(f'{path} 返回 {status}，预期 {expected}')
    return body


try:
    docker('volume', 'create', volume)
    docker('run', '--rm', '--mount', f'type=volume,source={volume},target=/app/state',
           args.image, 'homeai', 'init-key')
    docker('run', '-d', '--name', name, '--read-only', '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m',
           '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--pids-limit', '128',
           '--memory', '512m', '--mount', f'type=volume,source={volume},target=/app/state',
           '-p', '127.0.0.1::8000', args.image)
    config = json.loads(docker('inspect', name))[0]
    port = config['NetworkSettings']['Ports']['8000/tcp'][0]['HostPort']
    base = 'http://127.0.0.1:' + port
    for attempt in range(60):
        try:
            read(base, '/health/live')
            break
        except (OSError, RuntimeError):
            if attempt == 59:
                raise RuntimeError('镜像没有在 30 秒内就绪') from None
            time.sleep(0.5)
    html = read(base, '/admin/').decode()
    assets = re.findall(r'(?:src|href)="([^"]+\.(?:js|css))"', html)
    if not assets:
        raise RuntimeError('管理页面缺少真实构建资源')
    for asset in assets:
        if not asset.startswith('/admin/assets/'):
            raise RuntimeError('构建资源未使用同源管理路径')
        if not read(base, asset):
            raise RuntimeError('构建资源为空')
    read(base, '/api/v1/me', 401)
    read(base, '/docs', 404)
    read(base, '/admin/../state/master.key', 404)
    details = json.loads(docker('exec', name, 'python', '-c',
        'import os,json,homeai;from homeai.config import Settings;'
        'print(json.dumps({"uid":os.getuid(),"package":homeai.__file__,"admin":str(Settings().admin_dist)}))'))
    if details['uid'] != 10001 or 'site-packages' not in details['package']:
        raise RuntimeError('未验证到非 root 的实际安装包')
    print(json.dumps({'image': args.image, 'uid': details['uid'], 'installed_package': True,
        'admin_http': 200, 'assets_verified': len(assets), 'anonymous_api': 401,
        'production_docs': 404, 'read_only_root': True}, ensure_ascii=False))
finally:
    # 只清理本次 UUID 命名的临时容器和卷，不触碰部署数据。
    subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(['docker', 'volume', 'rm', volume], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
