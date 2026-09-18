"""为已绑定实例申请或恢复同一 ACME 订单，结果暂存供后续部署核查。"""
import argparse,json,logging
from types import SimpleNamespace
from homeai.acme_certificates import issue,PRODUCTION,STAGING
from homeai.acme_dns import RemoteDNS
from homeai.config import Settings
from homeai.crypto import Vault

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--production',action='store_true',help='使用正式 CA；默认使用 staging')
p.add_argument('--agree-tos',action='store_true',help='明确接受所选 CA 服务条款')
p.add_argument('--status',action='store_true',help='只读取加密订单状态，不发起网络请求')
a=p.parse_args()
if not a.agree_tos and not a.status:p.error('需要明确提供 --agree-tos')
logging.basicConfig(level=logging.WARNING)
s=Settings();app=SimpleNamespace(settings=s,vault=Vault.from_file(s.master_key_file))
try:
    if a.status:
        root=s.state_dir/'acme';path=root/'operation.enc'
        try:operation=app.vault.open(path.read_text(),'acme-operation')
        except FileNotFoundError:operation={'phase':'idle'}
        view={key:operation[key] for key in ('phase','domain','directory','order_uri','error_type','acme_error_code','cleanup_error_type') if key in operation}
        view['pending_dns_cleanup']=(root/'dns-pending.enc').exists()
        print(json.dumps(view,ensure_ascii=False));raise SystemExit(0)
    dns=RemoteDNS(app)
    result=issue(app,dns.domain,dns,directory=PRODUCTION if a.production else STAGING,agree_tos=a.agree_tos)
    print(json.dumps(result,ensure_ascii=False))
except Exception as exc:
    print(json.dumps({'status':'failed','error_type':type(exc).__name__,
        'message':'申请未完成；运行证书保持原样。请核查平台配置、DNS 和加密订单日志。'},ensure_ascii=False))
    raise SystemExit(1) from None
