"""初始化 Graphiti 专属开发凭据，不覆盖已有凭据，不写入公开仓库。"""
import os,secrets
from pathlib import Path
root=Path(__file__).resolve().parents[1]
path=root/'.env.graphiti'
if path.exists():
    if path.is_symlink() or path.stat().st_mode&0o077:raise SystemExit('配置权限必须为 0600')
    print('已有 Graphiti 配置，未覆盖。')
else:
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:
        f.write('HOMEAI_NEO4J_PASSWORD='+secrets.token_urlsafe(36)+'\n')
    print('已生成 Graphiti 独立配置，凭据不回显。')
