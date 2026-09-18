"""下载固定官方 FST 资源并校验，避免将 Git LFS 指针当作模型文件。"""
import hashlib
import json
import os
from pathlib import Path
import httpx

root=Path(__file__).resolve().parents[1]
manifest=json.loads((root/'providers/models/cosyvoice-source.json').read_text())
allowed={f'wetext/{lang}/tn/{name}.fst' for lang in ('zh','en') for name in ('tagger','verbalizer')}
files=[item for item in manifest['files'] if item['path'] in allowed]
if len(files)!=4:raise SystemExit('规范化资源清单不完整')
with httpx.Client(trust_env=False,follow_redirects=True,timeout=120) as client:
    for item in files:
        target=root/'state/vendor'/item['path']
        if target.is_symlink():raise SystemExit('模型目标不能是符号链接')
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest()==item['sha256']:
            continue
        relative=item['path'].removeprefix('wetext/')
        response=client.get('https://www.modelscope.cn/models/pengzhendong/wetext/resolve/'+manifest['wetext_revision']+'/'+relative)
        response.raise_for_status()
        data=response.content
        if len(data)!=item['bytes'] or hashlib.sha256(data).hexdigest()!=item['sha256']:
            raise SystemExit('上游资源与固定清单不一致')
        target.parent.mkdir(parents=True,exist_ok=True)
        temporary=target.with_suffix('.download')
        with os.fdopen(os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as stream:
            stream.write(data)
        os.replace(temporary,target)
print('四个文本规范化 FST 实体均通过 SHA256 校验。')
