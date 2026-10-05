"""Independent output checks and a compact snapshot of the user-supplied reference."""
import base64
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

from convert import Node, Parser
from layout import content_present, text, heading_key, section_number, style_values


def content_blocks(page):
    slots = [n for n in page.walk() if 'overflow-hidden' in n.attrs.get('class', '').split() and 'data-element-container' in n.attrs]
    if slots:
        return slots
    return [page]


def cover_headings(page):
    return {id(h) for c in content_blocks(page) for block in c.children if not isinstance(block, str)
            and block.attrs.get('data-element-type') == 'div' for h in block.walk()
            if h.attrs.get('data-element-type') == 'h'}


def toc_destination(row, headings):
    target = row.attrs.get('href', '').removeprefix('#')
    if target in headings:
        return target
    title = next(text(n) for n in row.walk() if n.attrs.get('class') == 'toc-title')
    choices = [key for key, n in headings.items() if heading_key(text(n)) == heading_key(title)]
    if len(choices) != 1 and section_number(title):
        choices = [key for key, n in headings.items() if section_number(text(n)) == section_number(title)]
    assert len(choices) == 1, f'Ambiguous/missing TOC destination: {title}'
    return choices[0]


def normalized_text(node):
    return re.sub(r'\s+', '', text(node))


def native_structure(node):
    """Compare nesting and editable attributes, independent of ID numbering."""
    attrs = {k: v for k, v in node.attrs.items() if k not in ('id', 'data-element-part-index', 'src')}
    if 'class' in attrs:
        attrs['class'] = sorted(attrs['class'].split())
    if 'style' in attrs:
        # The reference accidentally has a comma between border/bg declarations.
        attrs['style'] = style_values(attrs['style'].replace(';,', ';'))
    for k, v in list(attrs.items()):
        if v is None:
            attrs[k] = ''
    return [node.tag, attrs, [native_structure(c) for c in node.children if not isinstance(c, str)]]


def snapshot(source):
    tree = Parser(source).root
    pages = [n for n in tree.walk() if n.attrs.get('data-element-type') == 'page']
    locations = {n.attrs['id']: (i, normalized_text(n)) for i, p in enumerate(pages, 1)
                 for n in p.walk() if n.attrs.get('data-element-type') == 'h' and 'id' in n.attrs}
    headings = {n.attrs['id']: n for p in pages for n in p.walk() if n.attrs.get('data-element-type') == 'h' and id(n) not in cover_headings(p)}
    result = {'pages': [], 'images': [], 'toc': []}
    for page in pages:
        content = content_blocks(page)
        page_text = ''.join(normalized_text(n) for n in content)
        def without_toc_numbers(n):
            if isinstance(n, str):
                return n
            if n.attrs.get('class') == 'toc-page':
                return ''
            return ''.join(without_toc_numbers(c) for c in n.children)
        import html
        stable_text = re.sub(r'\s+', '', html.unescape(''.join(without_toc_numbers(n) for n in content)))
        types = Counter(n.attrs['data-element-type'] for c in content for n in c.walk() if 'data-element-type' in n.attrs)
        result['pages'].append({
            'page_structure_sha256': hashlib.sha256(json.dumps(native_structure(page), sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest(),
            'structure_sha256': hashlib.sha256(json.dumps([native_structure(c) for c in content], sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest(),
            'text_sha256': hashlib.sha256(page_text.encode('utf-8')).hexdigest(),
            'text_without_toc_numbers_sha256': hashlib.sha256(stable_text.encode('utf-8')).hexdigest(),
            'types': dict(types),
            'blocks': [[n.tag, n.attrs.get('data-element-type')] for c in content for n in c.children if isinstance(n, Node)],
        })
    for node in tree.walk():
        if node.attrs.get('data-element-type') == 'img':
            image = next(n for n in node.walk() if n.tag == 'img')
            width = next(n for n in node.walk() if 'adaptive-width' in n.attrs.get('class', ''))
            sizes = re.findall(r'--width-(lg|tb|mb):\s*([\d.]+)%', width.attrs.get('style', ''))
            result['images'].append({
                'sha256': hashlib.sha256(base64.b64decode(image.attrs['src'].split(',', 1)[1])).hexdigest(),
                'alt': image.attrs.get('alt'),
                'widths': {k: round(float(v), 8) for k, v in sizes},
            })
        if 'toc-row' in node.attrs.get('class', '').split():
            entry = {n.attrs['class']: text(n) for n in node.walk() if n.attrs.get('class') in ('toc-title', 'toc-page')}
            target_id = toc_destination(node, headings)
            if target_id in locations:
                entry['target_page'], entry['target_text'] = locations[target_id]
            result['toc'].append(entry)
    return result


def assert_reference(output, fixture):
    expected = json.loads(Path(fixture).read_text(encoding='utf-8'))['snapshot']
    actual = snapshot(Path(output).read_text(encoding='utf-8'))
    assert len(actual['pages']) == len(expected['pages']), 'Reference page count differs'
    for i, (got, want) in enumerate(zip(actual['pages'], expected['pages']), 1):
        keys = ('text_without_toc_numbers_sha256', 'types', 'blocks', 'structure_sha256', 'page_structure_sha256')
        assert all(got[k] == want[k] for k in keys), f'Reference page {i}: text, element types or block structure differs'
    assert actual['images'] == expected['images'], 'Reference image data, captions or responsive widths differ'
    assert len(actual['toc']) == len(expected['toc']), 'Reference TOC entry count differs'
    for got, want in zip(actual['toc'], expected['toc']):
        assert got['toc-title'] == want['toc-title'] and got.get('target_text') == want.get('target_text'), 'Reference TOC title or destination differs'
        assert got['target_page'] == want['target_page'], 'Reference TOC destination page differs'
        assert got['toc-page'] == str(got['target_page']), 'TOC page label differs from its actual destination'
    return len(actual['pages'])


def validate_layout(tree):
    pages = [n for n in tree.walk() if n.attrs.get('data-element-type') == 'page']
    ids = [n.attrs['id'] for n in tree.walk() if 'id' in n.attrs]
    assert len(ids) == len(set(ids)), 'Duplicate element IDs'
    for n in tree.walk():
        kind = n.attrs.get('data-element-type')
        assert 'lecture-content' not in n.attrs.get('class', '').split(), 'Unexpected content wrapper'
        if kind in ('text', 'h'):
            styles = style_values(n.attrs.get('style', ''))
            assert 'text-align' not in styles, 'Inline alignment blocks editor controls'
            assert styles.get('--lineHeight-lg') == '120%', 'Wrong editor line height'
        if kind in ('tableContainer', 'tableRow', 'tableCol'):
            assert ';,' not in n.attrs.get('style', ''), 'Invalid table CSS separator'
        if 'toc-row' in n.attrs.get('class', '').split():
            assert n.tag == 'div' and n.attrs['class'] == 'toc-row level-0', 'Wrong native TOC row'
    heading_pages, headings = {}, {}
    expected_targets = set()
    tocs = []
    for number, page in enumerate(pages, 1):
        contents = content_blocks(page)
        cover = cover_headings(page)
        assert contents and any(content_present(n) for n in contents), f'Empty page {number}'
        for n in page.walk():
            if n.attrs.get('data-field') == 'number':
                assert text(n) == str(number), f'Wrong page number on page {number}'
            if n.attrs.get('data-element-type') == 'h':
                if id(n) in cover:
                    continue
                heading_pages[n.attrs['id']] = number
                headings[n.attrs['id']] = n
                if 'cover-title' not in n.attrs.get('class', '') and text(n).casefold() not in ('содержание', 'оглавление'):
                    expected_targets.add(n.attrs['id'])
            if n.attrs.get('data-element-type') == 'toc':
                tocs.append(n)
    if expected_targets:
        assert len(tocs) == 1, f'Expected one TOC, found {len(tocs)}'
    linked = set()
    for toc in tocs:
        rows = [n for n in toc.walk() if 'toc-row' in n.attrs.get('class', '').split()]
        assert rows, 'Empty TOC'
        for row in rows:
            target = toc_destination(row, headings)
            assert target in heading_pages, f'Unresolved TOC link: {target}'
            number = next(n for n in row.walk() if n.attrs.get('class') == 'toc-page')
            assert text(number) == str(heading_pages[target]), f'Incorrect TOC page: {text(row)}'
            linked.add(target)
    assert expected_targets <= linked, 'TOC is missing headings: ' + ', '.join(text(headings[x]) for x in expected_targets - linked)
    return {'pages': len(pages), 'toc_entries': len(linked)}
