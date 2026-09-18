"""提供受管 HTTPS 入口并持续加载已验证的证书选择，不启动明文监听。"""
import argparse,asyncio
from pathlib import Path
from homeai.api import create_app
from homeai.managed_tls import serve
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--host',default='127.0.0.1');p.add_argument('--port',type=int)
p.add_argument('--test-ca',type=Path,help='仅允许绑定本机并处于非生产模式')
a=p.parse_args()
app=create_app();port=a.port if a.port is not None else app.state.settings.managed_https_port
if not 1024<=port<=65535:p.error('端口必须介于 1024 和 65535')
asyncio.run(serve(app,a.host,port,a.test_ca))
