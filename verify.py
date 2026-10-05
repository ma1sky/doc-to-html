"""Integration check against original DOCX files; no external dependencies."""
import argparse
import base64
from collections import Counter
import html
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET
from zipfile import ZipFile
from convert import DEFAULT_INPUT, ROOT, NS, Parser, q
from layout import text as node_text
from reference_checks import assert_reference, validate_layout


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument('source', nargs='?', type=Path, default=DEFAULT_INPUT)
    cli.add_argument('output', nargs='?', type=Path, default=ROOT / 'output')
    args = cli.parse_args()
    total = Counter()
    sources = sorted(f for f in args.source.rglob('*') if f.suffix.lower() == '.docx' and not f.name.startswith('~$'))
    assert sources, 'No DOCX files found'
    for source in sources:
        dest = args.output / source.relative_to(args.source).with_suffix('.html')
        tree = Parser(dest.read_text(encoding='utf-8')).root
        nodes = list(tree.walk())
        layout_result = validate_layout(tree)
        ids = [n.attrs['id'] for n in nodes if 'id' in n.attrs]
        assert len(ids) == len(set(ids)), f'Duplicate IDs: {dest}'
        contents = [n for n in nodes if 'lecture-content' in n.attrs.get('class', '').split()]
        def body_text(n):
            if isinstance(n, str):
                return n
            if n.attrs.get('data-element-type') == 'toc':
                return ''
            return ''.join(body_text(c) for c in n.children)
        rendered = ''.join(body_text(n) for n in contents)
        actual = re.sub(r'\s+', '', html.unescape(rendered))
        with ZipFile(source) as z:
            body = ET.fromstring(z.read('word/document.xml')).find('w:body', NS)
            # TOC page numbers are recalculated; compare document body independently.
            # Source TOC entries occupy the paragraphs between its title and body.
            toc_text_nodes, in_toc = set(), False
            for paragraph in body.findall('.//w:p', NS):
                parts = paragraph.findall('.//w:t', NS)
                paragraph_text = ''.join(t.text or '' for t in parts).strip()
                if paragraph_text.casefold() in ('содержание', 'оглавление'):
                    in_toc = True
                elif in_toc:
                    if not paragraph_text or re.search(r'\d\s*$', paragraph_text):
                        toc_text_nodes.update(id(t) for t in parts)
                    else:
                        in_toc = False
            pos = 0
            for text in body.findall('.//w:t', NS):
                if id(text) in toc_text_nodes:
                    continue
                expected = re.sub(r'\s+', '', text.text or '')
                if expected:
                    found = actual.find(expected, pos)
                    assert found >= 0, f'Missing/reordered text in {source.name}: {expected[:70]}'
                    pos = found + len(expected)
                    total['text_characters'] += len(expected)
            images = [n for n in nodes if n.tag == 'img']
            blips = body.findall('.//a:blip', NS)
            assert len(images) == len(blips), f'Image count: {source}'
            rels = {n.get('Id'): n.get('Target') for n in ET.fromstring(z.read('word/_rels/document.xml.rels'))}
            import posixpath
            from urllib.parse import unquote
            expected_images = Counter(z.read(posixpath.normpath(posixpath.join('word', unquote(rels[b.get(q('r:embed'))]))).lstrip('/')) for b in blips)
            actual_images = Counter(base64.b64decode(n.attrs['src'].split(',', 1)[1], validate=True) for n in images)
            assert expected_images == actual_images, f'Image bytes changed: {source}'
            tables = [n for n in nodes if n.attrs.get('data-element-type') == 'tableContainer']
            assert len(tables) == len(body.findall('.//w:tbl', NS)), f'Table count: {source}'
            formula_count = len(body.findall('.//m:oMath', NS))
            assert len([n for n in nodes if n.tag == 'math']) == formula_count, f'Formula count: {source}'
            total.update(files=1, images=len(images), tables=len(tables), formulas=formula_count)
            total.update(layout_result)
    fixture = ROOT / 'reference-lecture-1.json'
    reference_info = json.loads(fixture.read_text(encoding='utf-8'))
    reference_output = args.output / reference_info['output_file']
    if reference_output.exists():
        total['reference_pages_matched'] = assert_reference(reference_output, fixture)
    print('PASS:', dict(total))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
