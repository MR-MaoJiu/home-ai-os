import base64
import hashlib
import json
import os
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class Vault:
    """每份载荷独立 DEK，主密钥仅包装 DEK，AAD 绑定所有者与用途。"""
    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("主密钥必须为 32 字节")
        self.kek = AESGCM(key)

    @classmethod
    def from_file(cls, path: Path):
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise RuntimeError("主密钥文件必须为私有普通文件，权限 0600")
        return cls(path.read_bytes())

    def seal(self, value, context: str) -> str:
        key, iv, wrap_iv = os.urandom(32), os.urandom(12), os.urandom(12)
        aad = context.encode()
        wrapped = self.kek.encrypt(wrap_iv, key, aad)
        ciphertext = AESGCM(key).encrypt(iv, canonical(value), aad)
        return base64.b64encode(wrap_iv + wrapped + iv + ciphertext).decode()

    def open(self, value: str, context: str):
        raw = base64.b64decode(value, validate=True)
        key = self.kek.decrypt(raw[:12], raw[12:60], context.encode())
        return json.loads(AESGCM(key).decrypt(raw[60:72], raw[72:], context.encode()))
