"""家庭端密文备用通道：只把解密并验证的设备请求送入原 Core API。"""
import asyncio
import base64
import hashlib
import json
import secrets
import time
from collections import OrderedDict

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from .crypto import canonical
from .db import Device, Principal, Base, Owned, scope
from sqlalchemy import String, Text, delete
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.exc import IntegrityError
from .direct_http import validate_request, HEADERS
from .relay_crypto import RelayKeys, MAX_CIPHER, MAX_CLEAR, decode, verify
from .remote import read_config
from .server_identity import identity, public_identity
from .private_files import private_write


class RelayReceipt(Owned, Base):
    __tablename__ = 'encrypted_relay_receipts'
    device_id: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    response_key: Mapped[str] = mapped_column(Text)
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires: Mapped[float]


class RelayBroker:
    def __init__(self, app, config, client):
        self.app,self.config,self.client=app,config,client

    async def request(self, method, path, value=None):
        from .connect_signalling import platform_origin
        if not path.startswith('/api/relay/') or '?' in path:raise ValueError('无效中继路径')
        raw=canonical(value) if value is not None else b''
        if len(raw)>MAX_CIPHER:raise ValueError('中继请求超过限制')
        _,key=identity(self.app)
        timestamp,nonce=str(int(time.time())),secrets.token_hex(16)
        proof='\n'.join(['homeai-connect-relay:v1','agent',self.config['instance_id'],timestamp,nonce,method,path,hashlib.sha256(raw).hexdigest()])
        headers={'content-type':'application/json','x-connect-id':self.config['instance_id'],'x-connect-credential':self.config['credential'],
            'x-connect-time':timestamp,'x-connect-nonce':nonce,'x-connect-signature':base64.b64encode(key.sign(proof.encode(),ec.ECDSA(hashes.SHA256()))).decode()}
        async with self.client.stream(method,platform_origin(self.config['portal_url'])+path,headers=headers,content=raw) as response:
            response.raise_for_status();content=bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content)>MAX_CIPHER:raise ValueError('中继响应超过限制')
            return json.loads(content)


async def execute(app,keys,device_id,envelope):
    with app.state.db() as db:
        device=db.get(Device,device_id)
        if not device or device.revoked or not db.get(Principal,device.user_id):raise PermissionError('设备已撤销')
        trusted=serialization.load_pem_public_key(device.public_key.encode())
        member=db.get(Principal,device.user_id)
        owner,household=member.id,member.household_id
        header=verify(trusted,b'homeai-relay-request:v1\n',envelope)
        request_hash=hashlib.sha256(canonical(envelope)).hexdigest()
        if header.get('device_id')!=device_id or header.get('server_id')!=keys.server_id:raise ValueError('身份不匹配')
        scope(db,owner,household)
        previous=db.get(RelayReceipt,header.get('id',''))
        if previous:
            if previous.device_id!=device_id or previous.request_hash!=request_hash or previous.expires<time.time():raise ValueError('请求标识已经使用')
            if previous.response:return json.loads(previous.response)
            from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
            public=X25519PublicKey.from_public_bytes(decode(previous.response_key,32))
            return keys.response(header,public,409,b'{"detail":"Request already dispatched; result requires reconciliation"}')
    header,clear,response_key=keys.open_request(envelope,trusted,device_id)
    metadata=clear['metadata'];body=decode(clear['body'],MAX_CLEAR)
    path,headers=validate_request(metadata)
    if metadata['id']!=header['id'] or metadata['size']!=len(body):raise ValueError('请求长度或标识不匹配')
    with app.state.db() as db:
        scope(db,owner,household)
        from .relay_crypto import b64
        db.add(RelayReceipt(id=header['id'],owner_id=owner,household_id=household,device_id=device_id,
            request_hash=request_hash,response_key=b64(response_key.public_bytes_raw()),expires=time.time()+300))
        try:db.commit()
        except IntegrityError:raise ValueError('请求已在另一进程处理') from None
    received=time.time()
    async def bound(scope,receive,send):
        scope['homeai.direct_device_id']=device_id
        scope['homeai.received_at']=received
        count=0
        async def bounded(message):
            nonlocal count
            if message['type']=='http.response.body':
                count+=len(message.get('body',b''))
                if count>1400000:raise ValueError('中继响应应使用分块读取')
            await send(message)
        await app(scope,receive,bounded)
    transport=httpx.ASGITransport(app=bound,raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport,base_url='https://homeai.relay',follow_redirects=False,trust_env=False) as client:
        async with asyncio.timeout(90):
            response=await client.request(metadata['method'],path,headers=headers,content=body)
    envelope=keys.response(header,response_key,response.status_code,response.content)
    with app.state.db() as db:
        scope(db,owner,household)
        receipt=db.get(RelayReceipt,header['id'])
        receipt.response=canonical(envelope).decode()
        db.commit()
    return envelope


async def run_agent(app):
    cache=OrderedDict();tasks={};keys=None;stamp=None;published=0;active_broker=None
    async with httpx.AsyncClient(timeout=20,trust_env=False,follow_redirects=False,limits=httpx.Limits(max_connections=8,max_keepalive_connections=4)) as client:
        try:
            while True:
                try:
                    config=read_config(app.state)
                    from .remote import relay_enabled
                    if not config or not config.get('enabled') or not relay_enabled(app.state,config):
                        if active_broker is not None:
                            await active_broker.request('DELETE','/api/relay/key')
                            active_broker=None;published=0
                        private_write(app.state.settings.state_dir/'relay-status.json',json.dumps({'state':'disabled','checked_at':time.time()}))
                        await asyncio.sleep(2);continue
                    current=(config['instance_id'],config['credential'],config['portal_url'])
                    if current!=stamp:
                        for task in tasks.values():task.cancel()
                        if tasks:await asyncio.gather(*tasks.values(),return_exceptions=True)
                        tasks.clear();cache.clear()
                        server_id,key=identity(app.state);public=public_identity(app.state)
                        keys=RelayKeys(key,server_id,public['namespace_anchor']);stamp=current;published=0
                    broker=RelayBroker(app.state,config,client)
                    active_broker=broker
                    if time.time()-published>10:
                        await broker.request('POST','/api/relay/key',keys.advertisement());published=time.time()
                    while cache and next(iter(cache.values()))[0]<time.time():cache.popitem(last=False)
                    # 已满时先等现有请求完成；不能先领取再丢弃，让新请求白等租约到期。
                    if len(tasks)>=4:
                        await asyncio.wait(tuple(tasks.values()),timeout=.25,return_when=asyncio.FIRST_COMPLETED)
                        continue
                    incoming=await broker.request('GET','/api/relay/requests')
                    for entry in incoming.get('requests',[])[:1]:
                        envelope=entry['envelope'];identifier=envelope['payload']['id']
                        if identifier in tasks:continue
                        if len(tasks)>=4:continue
                        async def handle(device_id, envelope, identifier, broker=broker, keys=keys):
                            try:
                                request_hash=hashlib.sha256(canonical(envelope)).hexdigest()
                                previous=cache.get(identifier)
                                if previous:
                                    if previous[1]!=request_hash:raise ValueError('重复请求内容不同')
                                    response=previous[2]
                                else:
                                    response=await execute(app,keys,device_id,envelope)
                                    cache[identifier]=(time.time()+120,request_hash,response)
                                    while len(cache)>16:cache.popitem(last=False)
                                await broker.request('POST','/api/relay/responses/'+identifier,response)
                            except Exception:
                                # 未知副作用结果不重发，HTTP设备nonce也会拒绝旧请求。
                                pass
                            finally:tasks.pop(identifier,None)
                        tasks[identifier]=asyncio.create_task(handle(entry['device_id'],envelope,identifier))
                    private_write(app.state.settings.state_dir/'relay-status.json',json.dumps({'state':'ready','checked_at':time.time()}))
                except asyncio.CancelledError:raise
                except Exception as error:
                    private_write(app.state.settings.state_dir/'relay-status.json',json.dumps({'state':'unavailable','error_type':type(error).__name__,'checked_at':time.time()}))
                    await asyncio.sleep(2)
        finally:
            for task in tasks.values():task.cancel()
            if tasks:await asyncio.gather(*tasks.values(),return_exceptions=True)
