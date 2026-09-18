"""固定视觉模型与投影器，关闭网页入口和正文日志，仅供本机桥接调用。"""
import os,shutil,subprocess,sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
subprocess.run([sys.executable,str(root/'scripts/verify_provider_models.py'),'--manifest',str(root/'providers/models/vision-qwen2.json'),'--root',str(root/'state/models/vision-qwen2')],check=True)
binary=shutil.which('llama-server')
if not binary:raise SystemExit('需要 llama.cpp b8460 或经过重新验收的版本')
env={key:os.environ[key] for key in ('PATH','HOME','TMPDIR','LANG') if key in os.environ}
version=subprocess.run([binary,'--version'],capture_output=True,text=True,env=env,check=True)
if 'version: 8460 (b1c70e2e5)' not in version.stdout+version.stderr:raise SystemExit('需要已验证的 llama.cpp b8460 (b1c70e2e5)；升级必须重新验收')
name='Qwen2-VL-2B-Instruct-Q4_K_M.gguf'
os.execve(binary,[binary,'-m',str(root/'state/models/vision-qwen2'/name),'--mmproj',str(root/'state/models/vision-qwen2/mmproj-Qwen2-VL-2B-Instruct-Q8_0.gguf'),'--host','127.0.0.1','--port','58087','-c','8192','--parallel','1','--alias',name,'--jinja','--image-max-tokens','2048','--no-webui','--log-disable'],env)
