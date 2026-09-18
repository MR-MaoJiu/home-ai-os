"""使用固定 frpc 可执行文件维持隧道，不解释任何远端命令。"""
import asyncio,base64,json,os,secrets,time
from pathlib import Path
import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from .config import Settings
from .crypto import Vault
from .remote import identity,read_config,private_write
from types import SimpleNamespace

async def stop_process(process):
    if process and process.returncode is None:
        try:process.terminate()
        except ProcessLookupError:pass
        try:await asyncio.wait_for(process.wait(),timeout=10)
        except asyncio.TimeoutError:process.kill();await process.wait()

async def main():
    settings=Settings();app=SimpleNamespace(settings=settings,vault=Vault.from_file(settings.master_key_file))
    binary=Path(os.environ.get('HOMEAI_FRPC_PATH',str(settings.state_dir/'bin/frpc'))).resolve()
    if not binary.is_file():raise RuntimeError('请先安装并校验 frpc，设置 HOMEAI_FRPC_PATH')
    process=None;lease=None;renew_at=0;active_config=None
    try:
        async with httpx.AsyncClient(timeout=20,trust_env=False,follow_redirects=False) as client:
            while True:
                config=read_config(app)
                if not config or not config.get('enabled'):
                    await stop_process(process)
                    process=None;lease=None;active_config=None
                    private_write(settings.state_dir/'remote-status.json',json.dumps({'state':'disabled'}))
                    await asyncio.sleep(2);continue
                try:
                    from .certificate_store import ready_for_remote
                    from urllib.parse import urlparse
                    if not ready_for_remote(app,urlparse(config['url']).hostname,config['local_https_port']):
                        await stop_process(process);process=None;lease=None;renew_at=0
                        private_write(settings.state_dir/'remote-status.json',json.dumps({'state':'waiting_certificate','checked_at':time.time()}))
                        await asyncio.sleep(5);continue
                    current_config=(config['instance_id'],config['local_https_port'],config['portal_url'],config['relay_ca'],config['credential'])
                    if active_config!=current_config:
                        await stop_process(process)
                        process=None;lease=None;renew_at=0;active_config=current_config
                    if time.time()>=renew_at:
                        _,key=identity(app);timestamp=int(time.time());nonce=secrets.token_hex(16)
                        proof=f"{config['instance_id']}\n{timestamp}\n{nonce}"
                        response=await client.post(config['portal_url']+'/api/agent/lease',json={'instance_id':config['instance_id'],'credential':config['credential'],'lease':lease['token'] if lease else None,'timestamp':timestamp,'nonce':nonce,'signature':base64.b64encode(key.sign(proof.encode(),ec.ECDSA(hashes.SHA256()))).decode()})
                        response.raise_for_status();new_lease=response.json()
                        from urllib.parse import urlparse
                        if new_lease.get('domain')!=urlparse(config['url']).hostname:raise ValueError('租约域名与家庭确认的绑定不一致')
                        changed=not lease or lease['token']!=new_lease['token'];lease=new_lease;renew_at=time.time()+120
                        if changed:
                            await stop_process(process);process=None
                    if not process or process.returncode is not None:
                        directory=settings.state_dir/'remote';directory.mkdir(parents=True,exist_ok=True)
                        private_write(directory/'relay.crt',config['relay_ca'])
                        quoted=lambda s:json.dumps(str(s))
                        toml=f'''serverAddr = {quoted(lease['relay_host'])}
serverPort = {int(lease['relay_port'])}
transport.tls.enable = true
transport.tls.trustedCaFile = {quoted((directory/'relay.crt').resolve())}
transport.tls.serverName = {quoted(lease['relay_host'])}
metadatas.lease = {quoted(lease['token'])}
[[proxies]]
name = {quoted(config['instance_id'])}
type = "https"
localIP = "127.0.0.1"
localPort = {int(config['local_https_port'])}
customDomains = [{quoted(lease['domain'])}]
'''
                        private_write(directory/'frpc.toml',toml)
                        process=await asyncio.create_subprocess_exec(str(binary),'-c',str(directory/'frpc.toml'),stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
                    private_write(settings.state_dir/'remote-status.json',json.dumps({'state':'lease_active','url':lease['url'],'checked_at':time.time(),'process_running':process.returncode is None}))
                except Exception as exc:
                    await stop_process(process)
                    process=None;renew_at=0
                    private_write(settings.state_dir/'remote-status.json',json.dumps({'state':'disconnected','error_type':type(exc).__name__,'checked_at':time.time()}))
                await asyncio.sleep(5)
    finally:
        await stop_process(process)
if __name__=='__main__':asyncio.run(main())
