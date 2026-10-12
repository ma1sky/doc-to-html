"""Reproduce the edited lecture-10 reference and restore its image from DOCX.

The reference contains manual wording and page boundaries, so this profile is
valid only for the DOCX revision used to create it.
"""
import base64
import hashlib
import mimetypes
from pathlib import Path
import posixpath
import re
from urllib.parse import unquote
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from convert import NS, Parser, q


SOURCE_NAME = 'Лекция 10 - Процесс удаления фоторезиста.docx'
SOURCE_SHA256 = '7d9b5474adbe7a895c6a0e67162ff90662ebc6e8f5c5fb07a597ad0e37dd077a'


def render_reference(source: Path, reference: Path, expected_sha256: str):
    actual_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    if actual_hash != expected_sha256:
        raise ValueError(
            'DOCX изменился после создания эталона. Для новой версии нужен '
            'новый эталон или запуск с --ignore-etalon.'
        )
    with ZipFile(source) as archive:
        document = ET.fromstring(archive.read('word/document.xml'))
        relations = {
            node.get('Id'): node.get('Target')
            for node in ET.fromstring(archive.read('word/_rels/document.xml.rels'))
        }
        images = []
        for blip in document.findall('.//a:blip', NS):
            target = relations[blip.get(q('r:embed'))]
            media_path = posixpath.normpath(
                posixpath.join('word', unquote(target))
            ).lstrip('/')
            data = archive.read(media_path)
            mime = mimetypes.guess_type(media_path)[0] or 'application/octet-stream'
            images.append('data:' + mime + ';base64,' + base64.b64encode(data).decode('ascii'))

    html = reference.read_text(encoding='utf-8')
    tags = list(re.finditer(r'<img\b[^>]*>', html, flags=re.I | re.S))
    if len(tags) != len(images):
        raise ValueError(f'Рисунков в эталоне: {len(tags)}, в DOCX: {len(images)}')
    if not tags:
        raise ValueError('В эталоне лекции 10 ожидается рисунок')
    for tag, uri in reversed(list(zip(tags, images))):
        original = tag.group()
        src = re.search(r'\bsrc=(["\'])(.*?)\1', original, flags=re.I | re.S)
        if src is None or src.group(2) != '...':
            raise ValueError('Рисунок эталона должен содержать src="..."')
        replacement = original[:src.start(2)] + uri + original[src.end(2):]
        html = html[:tag.start()] + replacement + html[tag.end():]

    tree = Parser(html).root
    pages = [node for node in tree.walk() if node.attrs.get('data-element-type') == 'page']
    ids = [node.attrs['id'] for node in tree.walk() if 'id' in node.attrs]
    if len(ids) != len(set(ids)):
        raise ValueError('В эталоне повторяются ID элементов')
    if not pages or any(page.attrs.get('data-element-template') != 'base' for page in pages):
        raise ValueError('Эталон не содержит страницы типа base')
    if sum(node.attrs.get('data-element-type') == 'toc' for node in tree.walk()) != 1:
        raise ValueError('В эталоне должно быть одно оглавление')
    return html, {'pages': len(pages), 'images': len(images), 'reference': reference.name}

