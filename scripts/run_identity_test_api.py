"""只运行隔离测试库的真实 TLS 身份验收端点，不修改业务证书。"""
import argparse,json
from pathlib import Path
import uvicorn
from homeai.api import create_app
from homeai.config import Settings
p=argparse.ArgumentParser();p.add_argument('--fixture',type=Path,required=True);p.add_argument('--mode',choices=['original','renewed','mismatch'],required=True);a=p.parse_args()
root=Path(json.loads(a.fixture.read_text())['root'])
s=Settings();s.database_url=s.database_url.rsplit('/',1)[0]+'/homeai_test';s.state_dir=root/'home'
s.identity_certificate_file=root/('original.crt' if a.mode=='mismatch' else a.mode+'.crt')
s.server_addresses=['https://localhost:58444','https://localhost:58445']
uvicorn.run(create_app(s),host='127.0.0.1',port={'original':58444,'renewed':58445,'mismatch':58446}[a.mode],
            ssl_keyfile=str(root/(a.mode+'.key')),ssl_certfile=str(root/(a.mode+'.crt')),access_log=False)
