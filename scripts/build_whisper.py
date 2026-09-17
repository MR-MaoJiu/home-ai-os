"""从固定官方提交构建 whisper.cpp；不覆盖已有源码修改。"""
import subprocess
from pathlib import Path
root=Path(__file__).resolve().parents[1]
source=root/'state/whisper.cpp'
revision='371b5a7561823ab2bb32142d2751e35e7534727b'
if not source.exists():
    subprocess.run(['git','clone','--depth','1','--branch','v1.9.3','https://github.com/ggml-org/whisper.cpp.git',str(source)],check=True)
actual=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
if actual!=revision:raise SystemExit('源码提交与已验证版本不符')
if subprocess.check_output(['git','-C',str(source),'status','--porcelain'],text=True).strip():raise SystemExit('源码存在修改，先核查再构建')
subprocess.run(['cmake','-S',str(source),'-B',str(source/'build'),'-DCMAKE_BUILD_TYPE=Release','-DWHISPER_BUILD_TESTS=OFF','-DWHISPER_CURL=OFF'],check=True)
subprocess.run(['cmake','--build',str(source/'build'),'--target','whisper-server','-j','4'],check=True)
