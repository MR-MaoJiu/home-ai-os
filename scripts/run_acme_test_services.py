"""运行官方 Pebble 与真实 DNS 挑战服务，仅监听本机，不允许跳过验证。"""
import json,os,signal,subprocess,time
from pathlib import Path
root=Path(__file__).resolve().parents[1]
source=root/'state/vendor/pebble'
revision='b1e1ca4f3c30abb64111adaca4544bc5374cc306'
if not (source/'.git').exists() or subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()!=revision:
    raise SystemExit('请先取得 README 指定的 Pebble v2.10.1 固定源码')
subprocess.run(['git','-C',str(source),'diff','--exit-code','HEAD'],check=True,stdout=subprocess.DEVNULL)
binary=root/'state/bin';binary.mkdir(parents=True,exist_ok=True)
for name in ('pebble','pebble-challtestsrv'):
    subprocess.run(['go','build','-o',str(binary/name),'./cmd/'+name],cwd=source,check=True)
settings=json.loads((source/'test/config/pebble-config.json').read_text())
settings['pebble'].update(listenAddress='127.0.0.1:51400',managementListenAddress='127.0.0.1:51401',
    certificate=str(source/'test/certs/localhost/cert.pem'),privateKey=str(source/'test/certs/localhost/key.pem'))
directory=root/'state/acme-test';directory.mkdir(parents=True,exist_ok=True);config=directory/'pebble.json';config.write_text(json.dumps(settings))
env={key:os.environ[key] for key in ('PATH','HOME','TMPDIR','LANG') if key in os.environ}
env['PEBBLE_VA_NOSLEEP']='1';env['PEBBLE_AUTHZREUSE']='0'
def stop(signum,frame):raise KeyboardInterrupt
signal.signal(signal.SIGTERM,stop)
processes=[]
try:
    processes.append(subprocess.Popen([str(binary/'pebble-challtestsrv'),'-http01','','-https01','','-doh','','-tlsalpn01','',
        '-dnsserver','127.0.0.1:51453','-management','127.0.0.1:51455'],env=env))
    processes.append(subprocess.Popen([str(binary/'pebble'),'-config',str(config),'-dnsserver','127.0.0.1:51453'],env=env))
    while all(process.poll() is None for process in processes):time.sleep(1)
    raise SystemExit('测试 CA 或 DNS 服务退出，请检查端口及日志')
except KeyboardInterrupt:pass
finally:
    for process in processes:
        if process.poll() is None:process.terminate()
    for process in processes:
        try:process.wait(timeout=10)
        except subprocess.TimeoutExpired:process.kill();process.wait()
