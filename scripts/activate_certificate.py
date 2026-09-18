"""选择已验证证书包，由受管 HTTPS 服务加载后才计为实际启用。"""
import argparse,json
from pathlib import Path
from types import SimpleNamespace
from homeai.config import Settings
from homeai.crypto import Vault
from homeai.certificate_store import select_bundle,runtime_status,rollback_bundle
p=argparse.ArgumentParser(description=__doc__)
mode=p.add_mutually_exclusive_group(required=True)
mode.add_argument('--bundle',type=Path)
mode.add_argument('--status',action='store_true')
mode.add_argument('--rollback',help='重新选择仍有效的历史快照标识')
p.add_argument('--test-ca',type=Path,help='仅本机开发验证可使用的明确测试 CA')
a=p.parse_args();s=Settings();app=SimpleNamespace(settings=s,vault=Vault.from_file(s.master_key_file))
try:
    if a.status:print(json.dumps(runtime_status(app),ensure_ascii=False))
    elif a.rollback:print(json.dumps(rollback_bundle(app,a.rollback,a.test_ca),ensure_ascii=False))
    elif a.bundle:print(json.dumps(select_bundle(app,a.bundle,a.test_ca),ensure_ascii=False))
    else:p.error('需要 --bundle 或 --status')
except Exception as error:
    print(json.dumps({'status':'rejected','error_type':type(error).__name__},ensure_ascii=False));raise SystemExit(1) from None
