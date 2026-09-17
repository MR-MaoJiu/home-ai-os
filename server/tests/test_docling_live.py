"""真实 Docling HTTP 服务、附件加密、规范正文与来源删除验收。"""
import io
import os
import uuid
import zipfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from homeai.api import create_app
from homeai.config import Settings
from homeai.db import Provider, Secret, Principal, scope, uid
from homeai.contracts import ProviderManifest
from homeai.runtime import run_task
from conftest import SignedClient

pytestmark = pytest.mark.skipif(os.environ.get('HOMEAI_DOCLING_TEST') != '1', reason='需要独立 Docling HTTP 服务、PostgreSQL 和 OPA')


def docx_bytes():
    target = io.BytesIO()
    with zipfile.ZipFile(target, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr('_rels/.rels', '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr('word/document.xml', '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>家庭会议记录</w:t></w:r></w:p><w:p><w:r><w:t>周六下午三点检查备份。</w:t></w:r></w:p></w:body></w:document>')
    return target.getvalue()


def upload(user, filename, content):
    request = user.client.build_request('POST', '/api/v1/files', files={'file': (filename, content)})
    raw = request.read()
    headers = user.headers('POST', '/api/v1/files', raw)
    headers['content-type'] = request.headers['content-type']
    result = user.client.post('/api/v1/files', content=raw, headers=headers)
    assert result.status_code == 200, result.text
    return result.json()['id']


@pytest.mark.asyncio
@pytest.mark.parametrize('filename,content', [
    ('家庭会议.docx', docx_bytes()),
    ('家庭会议.md', '# 家庭会议记录\n\n周六下午三点检查备份。'.encode()),
    ('家庭会议.html', '<html><body><h1>家庭会议记录</h1><p>周六下午三点检查备份。</p></body></html>'.encode()),
    ('家庭会议.txt', '家庭会议记录\n周六下午三点检查备份。'.encode()),
    ('sample.pptx', None),
    ('sample.pdf', None),
    ('sample.png', None),
], ids=['docx','markdown','html','text','pptx','pdf','png'])
async def test_real_docling_upload_parse_and_delete(filename,content,tmp_path):
    if content is None:
        import subprocess
        subprocess.run(['state/venvs/docling/bin/python', 'server/tests/create_document_samples.py', str(tmp_path)], check=True)
        content = (tmp_path / filename).read_bytes()
    settings = Settings()
    settings.database_url = settings.database_url.rsplit('/', 1)[0] + '/homeai_test'
    app = create_app(settings)
    client = TestClient(app)
    user = SignedClient(client, app.state.db, household=str(uuid.uuid4()))
    other = SignedClient(client, app.state.db, household=str(uuid.uuid4()))
    provider_id = 'a.docling.live'
    secret_id = uid()
    with app.state.db() as db:
        principal = db.get(Principal, user.user_id)
        scope(db, user.user_id, principal.household_id)
        token = Path('state/provider-secrets/docling.token').read_text().strip()
        db.add(Secret(id=secret_id, owner_id=user.user_id, household_id=principal.household_id, provider_id=provider_id, value=app.state.vault.seal(token, user.user_id + ':secret:' + secret_id)))
        manifest = ProviderManifest(id=provider_id, version='2.128.0', adapter='http', endpoint='http://127.0.0.1:8103', capabilities={'document.parse@v1':'/invoke/parse'}, allowed_hosts=['127.0.0.1'], secret_id=secret_id, timeout_seconds=300)
        row = db.get(Provider, provider_id)
        if row: row.manifest, row.enabled = manifest.model_dump_json(), True
        else: db.add(Provider(id=provider_id, manifest=manifest.model_dump_json(), enabled=True))
        db.commit()
    try:
        rid = upload(user, filename, content)
        assert other.request('POST', '/api/v1/files/' + rid + '/parse').status_code == 404
        response = user.request('POST', '/api/v1/files/' + rid + '/parse')
        assert response.status_code == 202, response.text
        tid = response.json()['id']
        assert user.request('POST', '/api/v1/files/' + rid + '/parse').json()['id'] == tid
        await run_task(app.state, tid, user.user_id)
        task = user.request('GET', '/api/v1/tasks/' + tid).json()
        assert task['status'] == 'SUCCEEDED', task
        expected = 'Saturday backup' if filename.endswith(('.pdf', '.png')) else '周六下午三点检查备份'
        assert expected in task['result']['markdown']
        parsed_id = task['result']['record_id']
        parsed = user.request('GET', '/api/v1/data/' + parsed_id).json()
        assert parsed['kind'] == 'document.parsed'
        assert parsed['payload']['source_ids'] == [rid]
        assert parsed['payload']['parser_version'] == '2.128.0'
        assert other.request('GET', '/api/v1/data/' + parsed_id).status_code == 404
        deleted = user.request('DELETE', '/api/v1/data/' + rid)
        assert parsed_id in deleted.json()['deleted_ids']
        assert user.request('GET', '/api/v1/data/' + parsed_id).status_code == 404
    finally:
        with app.state.db() as db:
            db.get(Provider, provider_id).enabled = False
            db.commit()
