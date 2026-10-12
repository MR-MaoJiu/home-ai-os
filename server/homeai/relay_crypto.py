"""RFC 9180 HPKE 密文封装；长期签名身份与临时加密密钥分离。"""
import base64
import json
import re
import secrets
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, x25519
from pyhpke import CipherSuite, KEMKey, KEMId, KDFId, AEADId
from .crypto import canonical

SUITE = CipherSuite.new(KEMId.DHKEM_X25519_HKDF_SHA256, KDFId.HKDF_SHA256, AEADId.CHACHA20_POLY1305)
MAX_CIPHER = 4 * 1024 * 1024
MAX_CLEAR = 2 * 1024 * 1024


def b64(value): return base64.b64encode(value).decode('ascii')

def decode(value, limit):
    if not isinstance(value, str) or len(value) > (limit + 2) // 3 * 4: raise ValueError('密文格式无效')
    raw = base64.b64decode(value, validate=True)
    if len(raw) > limit: raise ValueError('密文超过限制')
    return raw


def sign(identity, domain, payload):
    return {'payload': payload, 'signature': b64(identity.sign(domain + canonical(payload), ec.ECDSA(hashes.SHA256())))}


def verify(identity, domain, envelope):
    if not isinstance(envelope, dict) or set(envelope) != {'payload','signature'} or not isinstance(envelope['payload'],dict):
        raise ValueError('密文信封无效')
    identity.verify(decode(envelope['signature'], 100), domain + canonical(envelope['payload']), ec.ECDSA(hashes.SHA256()))
    return envelope['payload']


def info(direction, server_id, request_id, key_id):
    return ('homeai-relay-hpke:v1|' + direction + '|' + server_id + '|' + request_id + '|' + key_id).encode()


def seal(public, value, context):
    raw = canonical(value)
    if len(raw) > MAX_CLEAR: raise ValueError('请求超过加密通道单包限制')
    enc, sender = SUITE.create_sender_context(KEMKey.from_pyca_cryptography_key(public), info=context)
    return b64(enc), b64(sender.seal(raw))


def unseal(private, enc, ciphertext, context):
    recipient = SUITE.create_recipient_context(decode(enc,32), KEMKey.from_pyca_cryptography_key(private), info=context)
    raw = recipient.open(decode(ciphertext,MAX_CLEAR+16))
    if len(raw) > MAX_CLEAR: raise ValueError('明文超过限制')
    value = json.loads(raw)
    if not isinstance(value,dict): raise ValueError('明文格式无效')
    return value


class RelayKeys:
    def __init__(self, identity, server_id, namespace):
        self.identity, self.server_id, self.namespace = identity, server_id, namespace
        self.keys = {}
        self.current = None

    def advertisement(self):
        current = time.time()
        self.keys = {key:value for key,value in self.keys.items() if value[1] + 120 > current}
        if self.current not in self.keys or self.keys[self.current][1] - current < 180:
            self.current = secrets.token_hex(16)
            self.keys[self.current] = (x25519.X25519PrivateKey.generate(), int(current) + 900)
        key, expires = self.keys[self.current]
        payload = {'version':1, 'server_id':self.server_id, 'namespace_anchor':self.namespace,
            'key_id':self.current, 'public_key':b64(key.public_key().public_bytes_raw()), 'expires':expires, 'transport_policy':'smart_e2e'}
        return sign(self.identity,b'homeai-relay-key:v1\n',payload)

    def open_request(self, envelope, device_public_key, expected_device_id):
        p = verify(device_public_key,b'homeai-relay-request:v1\n',envelope)
        if set(p) != {'version','id','server_id','key_id','device_id','expires','enc','ciphertext'} or p['version'] != 1:
            raise ValueError('请求字段无效')
        if not isinstance(p['id'],str) or not re.fullmatch('[0-9a-f]{32}',p['id']) or p['server_id'] != self.server_id or p['device_id'] != expected_device_id:
            raise ValueError('请求身份不匹配')
        if type(p['expires']) is not int or not time.time() < p['expires'] <= time.time()+125: raise ValueError('请求已过期')
        pair = self.keys.get(p['key_id'])
        if pair is None or pair[1]+120 <= time.time(): raise ValueError('加密会话已过期')
        value = unseal(pair[0],p['enc'],p['ciphertext'],info('request',self.server_id,p['id'],p['key_id']))
        if set(value) != {'metadata','body','response_public_key'}: raise ValueError('请求正文契约无效')
        response_key = x25519.X25519PublicKey.from_public_bytes(decode(value['response_public_key'],32))
        return p,value,response_key

    def response(self, request, public, status, body):
        enc,cipher = seal(public, {'status':status,'body':b64(body)}, info('response',self.server_id,request['id'],request['key_id']))
        payload = {'version':1,'id':request['id'],'server_id':self.server_id,'key_id':request['key_id'],
            'expires':int(time.time())+120,'enc':enc,'ciphertext':cipher}
        return sign(self.identity,b'homeai-relay-response:v1\n',payload)
