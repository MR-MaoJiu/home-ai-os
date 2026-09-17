"""通过真实 HTTPS 与 worker 检查本地聊天；验收设备完成后立即撤销。"""
import base64
import json
import ssl
import time
import uuid
from pathlib import Path
import httpx
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization,hashes
from homeai.config import Settings
from homeai.db import database,Principal,now
from homeai.security import credential
from homeai.crypto import digest

root=Path(__file__).resolve().parents[1]
user_id=json.loads((root/'state/bootstrap.json').read_text())['user_id']
_,factory=database(Settings().database_url)
with factory() as db:
    if not db.get(Principal,user_id):raise SystemExit('初始化用户不存在')
    pairing=credential(db,user_id,'pair',60)
    db.commit()
key=ec.generate_private_key(ec.SECP256R1())
def sign(raw):return base64.b64encode(key.sign(raw,ec.ECDSA(hashes.SHA256()))).decode()
context=ssl.create_default_context(cafile=str(root/'state/tls/server.crt'))
with httpx.Client(base_url='https://localhost:58443',verify=context,trust_env=False,timeout=30) as client:
    response=client.post('/api/v1/pair',json={'token':pairing,'public_key':key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode(),'signature':sign(('homeai-pair:'+pairing).encode()),'name':'本机验收设备（自动撤销）'})
    response.raise_for_status()
    token=response.json()['access_token']
    device=response.json()['device_id']
    def request(method,path,body=None):
        raw=json.dumps(body,ensure_ascii=False).encode() if body else b''
        stamp,nonce=str(now()),str(uuid.uuid4())
        proof='\n'.join([stamp,nonce,method,path,digest(raw),digest(token.encode())])
        r=client.request(method,path,content=raw,headers={'Authorization':'Bearer '+token,'X-HomeAI-Time':stamp,'X-HomeAI-Nonce':nonce,'X-HomeAI-Signature':sign(proof.encode()),'Content-Type':'application/json'})
        r.raise_for_status()
        return r.json()
    try:
        task=request('POST','/api/v1/tasks',{'message':'你好，请用一句话回应。/no_think','idempotency_key':'live-smoke-'+str(uuid.uuid4()),'max_output_tokens':96})
        for _ in range(60):
            result=request('GET','/api/v1/tasks/'+task['id'])
            if result['status'] not in {'RECEIVED','EXECUTING'}:break
            time.sleep(1)
        if result['status']!='SUCCEEDED':raise SystemExit('真实链路失败：'+result['status'])
        print(json.dumps({'https':'passed','device_auth':'passed','worker':'passed','model':'passed','task_id':task['id'],'completion_tokens':result['result']['usage']['completion_tokens']},ensure_ascii=False))
    finally:
        request('DELETE','/api/v1/devices/'+device)
        print('本机验收设备已撤销')
