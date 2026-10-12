"""安装固定版本、校验摘要的安卓连续采集组件；不安装桌面客户端或开启控制通道。"""
import argparse
import hashlib
import os
from pathlib import Path
import tempfile
import urllib.request

VERSION = '4.1'
SHA256 = 'deacb991ed2509715160ffdc7907e47b4160eb30d1566217e9047fd5b8850cae'
ROOT = Path(__file__).resolve().parents[1]
URL = f'https://github.com/Genymobile/scrcpy/releases/download/v{VERSION}/scrcpy-server-v{VERSION}'


def download(url, limit):
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'HomeAI-Media-Installer'}), timeout=45) as response:
        if response.geturl().split(':', 1)[0] != 'https':
            raise SystemExit('组件下载必须保持 HTTPS')
        value = response.read(limit + 1)
    if not value or len(value) > limit:
        raise SystemExit('组件响应大小无效')
    return value


def private_write(path, raw):
    descriptor, temporary = tempfile.mkstemp(prefix='.install-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(raw)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'check'])
    args = parser.parse_args()
    directory = ROOT / 'state' / 'android-media'
    path = directory / f'scrcpy-server-v{VERSION}.jar'
    if path.is_file() and not path.is_symlink() and hashlib.sha256(path.read_bytes()).hexdigest() == SHA256:
        print(f'连续视频组件已就绪：scrcpy {VERSION}，SHA256 校验通过')
        return
    if args.action == 'check':
        raise SystemExit('连续视频组件未就绪，请运行 scripts/android_media.py install')
    raw = download(URL, 2 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != SHA256:
        raise SystemExit('官方组件摘要不匹配，未安装')
    license_text = download(f'https://raw.githubusercontent.com/Genymobile/scrcpy/v{VERSION}/LICENSE', 65536)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_write(directory / 'LICENSE', license_text)
    private_write(path, raw)
    print(f'已安装 scrcpy {VERSION} 连续视频组件，SHA256 校验通过；上游许可保存在同目录 LICENSE')


if __name__ == '__main__':
    main()
