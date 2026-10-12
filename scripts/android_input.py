"""构建及在本机明确指定的设备上安装输入组件；不含网络权限或账号数据。"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'state' / 'android-input'
COMPONENT = 'dev.homeai.androidinput/.HomeAIInputService'


def run(argv):
    return subprocess.run([str(x) for x in argv], check=True, capture_output=True, text=True).stdout.strip()


def build(sdk, java):
    tools = sdk / 'build-tools' / '35.0.0'
    android = sdk / 'platforms' / 'android-35' / 'android.jar'
    source = ROOT / 'android-input'
    WORK.mkdir(parents=True, exist_ok=True, mode=0o700)
    classes = WORK / 'classes'
    if classes.exists():
        shutil.rmtree(classes)
    classes.mkdir()
    run([java/'bin/javac', '-source', '8', '-target', '8', '-bootclasspath', android, '-d', classes, *source.glob('src/**/*.java')])
    run([tools/'d8', '--lib', android, '--output', WORK, *classes.glob('**/*.class')])
    with zipfile.ZipFile(WORK/'homeai-touch.jar', 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.write(WORK/'classes.dex', 'classes.dex')
    (WORK/'homeai-touch.jar').chmod(0o600)
    run([tools/'aapt2', 'compile', '--dir', source/'res', '-o', WORK/'resources.zip'])
    unsigned = WORK/'input-unsigned.apk'
    run([tools/'aapt2', 'link', '-I', android, '--manifest', source/'AndroidManifest.xml', '-o', unsigned, WORK/'resources.zip'])
    with zipfile.ZipFile(unsigned, 'a') as archive:
        archive.write(WORK/'classes.dex', 'classes.dex')
    key = WORK/'signing.p12'
    if not key.exists():
        run([java/'bin/keytool', '-genkeypair', '-keystore', key, '-alias', 'homeai-input', '-keyalg', 'RSA', '-keysize', '2048', '-validity', '3650', '-storepass', 'local-build-only', '-keypass', 'local-build-only', '-dname', 'CN=Home AI Local Input'])
        key.chmod(0o600)
    apk = WORK/'homeai-input.apk'
    run([tools/'apksigner', 'sign', '--ks', key, '--ks-pass', 'pass:local-build-only', '--out', apk, unsigned])
    run([tools/'apksigner', 'verify', apk])
    print('输入组件已构建：', apk)
    return apk


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['build', 'install', 'restore'])
    parser.add_argument('--serial')
    parser.add_argument('--sdk', type=Path, default=Path(os.environ.get('ANDROID_SDK_ROOT', Path.home()/'Library/Android/sdk')))
    parser.add_argument('--java', type=Path, default=Path(os.environ.get('JAVA_HOME', '/Library/Java/JavaVirtualMachines/openjdk-17.jdk/Contents/Home')))
    args = parser.parse_args()
    if args.action == 'build':
        build(args.sdk, args.java); return
    if not args.serial or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', args.serial):
        parser.error('安装或恢复时必须显式指定本机设备序号')
    adb = shutil.which('adb')
    if not adb: parser.error('未安装 adb')
    prefix = [adb, '-s', args.serial]
    WORK.mkdir(parents=True, exist_ok=True, mode=0o700)
    saved = WORK/(args.serial.replace(':', '_')+'.json')
    if args.action == 'restore':
        value = json.loads(saved.read_text())['input_method']
        if not re.fullmatch(r'[A-Za-z0-9_./]+', value): parser.error('原输入法记录无效')
        run(prefix+['shell','ime','set',value]); print('已恢复原输入法'); return
    apk = build(args.sdk, args.java)
    original = run(prefix+['shell','settings','get','secure','default_input_method'])
    if original != COMPONENT:
        saved.write_text(json.dumps({'input_method':original})); saved.chmod(0o600)
    result = run(prefix+['install','-r',apk])
    if 'Success' not in result:
        raise SystemExit('设备未确认安装成功，请在安卓手机上检查安装提示。')
    for _ in range(20):
        if COMPONENT in run(prefix+['shell','ime','list','-a','-s']):
            break
        time.sleep(.5)
    else:
        raise SystemExit('输入法尚未完成系统注册，请在手机设置中启用后重试。')
    run(prefix+['shell','ime','enable',COMPONENT])
    run(prefix+['shell','ime','set',COMPONENT])
    if run(prefix+['shell','settings','get','secure','default_input_method']) != COMPONENT:
        raise SystemExit('系统未确认启用输入法，请在手机上选择 Home AI 远程输入。')
    print('本机指定设备的中文输入已启用；请另在 Android 本机策略登记 ime_serials。')


if __name__ == '__main__':
    main()
