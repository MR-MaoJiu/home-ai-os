import io
import pytest
from homeai.backup import encrypt_stream, decrypt_stream


def test_backup_roundtrip_and_tamper():
    payload=b'private data'*200000
    encrypted=io.BytesIO()
    encrypt_stream(io.BytesIO(payload),encrypted,b'x'*32)
    output=io.BytesIO()
    decrypt_stream(io.BytesIO(encrypted.getvalue()),output,b'x'*32)
    assert output.getvalue()==payload
    raw=bytearray(encrypted.getvalue());raw[40]^=1
    with pytest.raises(Exception):decrypt_stream(io.BytesIO(raw),io.BytesIO(),b'x'*32)
    with pytest.raises(Exception):decrypt_stream(io.BytesIO(encrypted.getvalue()[:-16]),io.BytesIO(),b'x'*32)
