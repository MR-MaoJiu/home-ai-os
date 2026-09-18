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


def test_archive_manifest_rejects_missing_changed_or_duplicate_files():
    import tarfile,json,hashlib
    from homeai.backup import verify_archive
    def archive(data=b'actual database bytes', duplicate=False):
        output=io.BytesIO()
        manifest={'format_version':2,'files':{'database.dump':{'bytes':len(b'actual database bytes'),'sha256':hashlib.sha256(b'actual database bytes').hexdigest()}}}
        with tarfile.open(fileobj=output,mode='w:gz') as tar:
            for name,content in [('database.dump',data),('manifest.json',json.dumps(manifest).encode())]+([('database.dump',data)] if duplicate else []):
                entry=tarfile.TarInfo(name);entry.size=len(content);tar.addfile(entry,io.BytesIO(content))
        return output
    assert verify_archive(archive())['manifest_verified']
    with pytest.raises(ValueError):verify_archive(archive(b'changed bytes'))
    with pytest.raises(ValueError):verify_archive(archive(duplicate=True))


def test_legacy_requires_explicit_opt_in_and_unsafe_paths_are_rejected():
    import tarfile
    from homeai.backup import verify_archive
    def make(names):
        output=io.BytesIO()
        with tarfile.open(fileobj=output,mode='w') as tar:
            for name in names:
                entry=tarfile.TarInfo(name);entry.size=1;tar.addfile(entry,io.BytesIO(b'x'))
        return output
    with pytest.raises(ValueError):verify_archive(make(['database.dump']))
    assert verify_archive(make(['database.dump']),allow_legacy=True)['manifest_verified'] is False
    with pytest.raises(ValueError):verify_archive(make(['database.dump','../outside']),allow_legacy=True)
