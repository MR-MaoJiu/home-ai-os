import base64
import json
import uuid
import pytest
import httpx
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization, hashes
from fastapi.testclient import TestClient
from homeai.api import create_app
from homeai.config import Settings
from homeai.crypto import Vault, digest
from homeai.db import Base, database, Principal, now
from homeai.security import credential
from homeai.policy import Policy


class SignedClient:
    def __init__(self, client, factory, household="h1", role="adult"):
        self.client = client
        self.factory = factory
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.user_id = str(uuid.uuid4())
        with factory() as db:
            db.add(Principal(id=self.user_id, household_id=household, name="测试成员", role=role))
            token = credential(db, self.user_id, "pair", 300)
            db.commit()
        public = self.key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        body = {"token":token,"public_key":public,"name":"测试设备","signature":self.sign(("homeai-pair:"+token).encode())}
        result = client.post('/api/v1/pair', json=body)
        assert result.status_code == 200, result.text
        self.token = result.json()['access_token']
        self.device_id = result.json()['device_id']

    def sign(self, data):
        return base64.b64encode(self.key.sign(data, ec.ECDSA(hashes.SHA256()))).decode()

    def headers(self, method, path, data):
        timestamp, nonce = str(now()), str(uuid.uuid4())
        proof = '\n'.join([timestamp,nonce,method,path,digest(data),digest(self.token.encode())])
        return {'authorization':'Bearer '+self.token,'x-homeai-time':timestamp,'x-homeai-nonce':nonce,'x-homeai-signature':self.sign(proof.encode()),'content-type':'application/json'}

    def request(self, method, path, body=None):
        path = httpx.URL(path).raw_path.decode()
        raw = json.dumps(body,ensure_ascii=False).encode() if body is not None else b''
        return self.client.request(method,path,content=raw,headers=self.headers(method,path,raw))


@pytest.fixture
def system(tmp_path):
    engine, factory = database('sqlite:///'+str(tmp_path/'test.db'),test=True)
    from homeai.schema import registered_metadata
    registered_metadata().create_all(engine)
    policy = Policy('http://opa',httpx.MockTransport(lambda r:httpx.Response(200,json={'result':True})))
    app = create_app(Settings(state_dir=tmp_path),Vault(b'k'*32),factory,policy)
    client=TestClient(app)
    yield app, client, factory
    engine.dispose()


@pytest.fixture
def alice(system):
    return SignedClient(system[1],system[2],role='infrastructure_owner')


@pytest.fixture(scope='session', autouse=True)
def isolated_postgres_name():
    """可为一次发布检查使用新测试库，不清空其他测试进程或家庭业务库。"""
    import os
    import re
    from sqlalchemy.engine import make_url
    from homeai import db as database_module
    name = os.environ.get('HOMEAI_TEST_DATABASE_NAME')
    if not name:
        yield
        return
    if not re.fullmatch(r'homeai_test_[a-z0-9_]+', name):
        raise ValueError('独立测试库名称必须以 homeai_test_ 开头')
    original = database_module.create_engine
    def create_engine_for_test(url, *args, **kwargs):
        parsed = make_url(url)
        if parsed.get_backend_name() == 'postgresql' and parsed.database == 'homeai_test':
            url = parsed.set(database=name)
        return original(url, *args, **kwargs)
    patch = pytest.MonkeyPatch()
    patch.setattr(database_module, 'create_engine', create_engine_for_test)
    try:
        yield
    finally:
        patch.undo()
