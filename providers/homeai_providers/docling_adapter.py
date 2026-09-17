"""离线 Docling 解析；文件只来自已验证的字节流，不接受 URL 或宿主路径。"""
import io
import os
import re
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from functools import lru_cache
from importlib.metadata import version
from fastapi import HTTPException

FORMATS = {'.pdf', '.docx', '.pptx', '.html', '.txt', '.md', '.png', '.jpg'}


def preflight(data, suffix):
    if not data:
        raise HTTPException(422, '空文件不能解析')
    if suffix in {'.docx', '.pptx'}:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                infos = archive.infolist()
                if len(infos) > 10000 or sum(item.file_size for item in infos) > 100 * 1024 * 1024:
                    raise HTTPException(413, '文档解压规模超过限制')
                for item in infos:
                    if item.filename.startswith('/') or '..' in Path(item.filename).parts:
                        raise HTTPException(422, '文档归档路径无效')
                    if item.filename.endswith(('.xml', '.rels')):
                        content = archive.read(item)
                        if re.search(br'<!\s*(DOCTYPE|ENTITY)', content, re.I):
                            raise HTTPException(422, '文档包含外部实体声明')
                        if item.filename.endswith('.rels'):
                            try:
                                relationships = ET.fromstring(content)
                            except ET.ParseError:
                                raise HTTPException(422, 'Office 关系文件无效') from None
                            if any(node.attrib.get('TargetMode', '').lower() == 'external' for node in relationships.iter()):
                                raise HTTPException(422, '请移除 Office 文档中的外部链接关系再解析')
        except zipfile.BadZipFile:
            raise HTTPException(422, 'Office 文件不是有效归档') from None
    elif suffix in {'.html', '.md'}:
        if re.search(br'<!\s*(DOCTYPE|ENTITY)', data, re.I) and suffix != '.html':
            raise HTTPException(422, '文件包含实体声明')
        if re.search(br'<!\s*ENTITY', data, re.I):
            raise HTTPException(422, '文件包含实体声明')
        if re.search(br'(?:src|srcset)\s*=|!\[', data, re.I):
            raise HTTPException(422, '请移除外部图片引用，图片应独立上传解析')


@lru_cache
def converter():
    if version('docling') != '2.128.0':
        raise RuntimeError('Docling SDK 版本与验收版本不符')
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption, ImageFormatOption
    path = Path(os.environ['DOCLING_ARTIFACTS_PATH'])
    options = PdfPipelineOptions(artifacts_path=path, enable_remote_services=False, allow_external_plugins=False, document_timeout=120, do_ocr=True, ocr_options=RapidOcrOptions(backend="onnxruntime", lang=["iso:zh"]))
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options), InputFormat.IMAGE: ImageFormatOption(pipeline_options=options)})


def parse(call, data):
    suffix = Path(call.arguments.get('filename', '')).suffix.lower()
    if suffix not in FORMATS:
        raise HTTPException(422, '文档格式不支持')
    preflight(data, suffix)
    if suffix in {'.png', '.jpg'}:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 30_000_000:
                raise HTTPException(413, '图片像素总数超过限制')
    if suffix in {'.pdf', '.png', '.jpg'} and not Path(os.environ['DOCLING_ARTIFACTS_PATH']).is_dir():
        raise HTTPException(503, '尚未安装本地 PDF/图片解析模型，拒绝自动联网下载')
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / ('input' + suffix)
        path.write_bytes(data)
        converted = converter().convert(path, max_num_pages=100, max_file_size=20 * 1024 * 1024)
        markdown = converted.document.export_to_markdown()
        if not markdown.strip():
            raise HTTPException(422, '文档中没有可提取正文')
        if len(markdown.encode()) > 3 * 1024 * 1024:
            raise HTTPException(413, '提取正文超过限制')
        return {'markdown': markdown, 'format': suffix[1:], 'parser_version': version('docling')}
