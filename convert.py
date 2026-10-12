"""Batch DOCX -> editor HTML. Python 3.10+, standard library only."""
from __future__ import annotations

import argparse
import base64
import copy
import html
from html.parser import HTMLParser
import json
import mimetypes
from pathlib import Path
import posixpath
import re
import sys
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = Path.home() / 'Downloads/Telegram Desktop/Лекции (1)/Лекции'
NS = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
      'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
      'wp': 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing',
      'm': 'http://schemas.openxmlformats.org/officeDocument/2006/math'}


def q(name):
    prefix, local = name.split(':')
    return '{' + NS[prefix] + '}' + local


def value(node, path, default=None, attr='w:val'):
    child = node.find(path, NS) if node is not None else None
    return child.get(q(attr), default) if child is not None else default


def esc(s):
    return html.escape(str(s), quote=True)


def adaptive(key, val):
    return ' '.join(f'--{key}-{device}: {val};' for device in ('lg', 'tb', 'mb'))


def classes(key, val):
    return ' '.join(f'{key}-{device}-{val}' for device in ('lg', 'tb', 'mb'))


def split_page_breaks(node):
    """Split a paragraph at Word's page markers, retaining run properties."""
    def marker(n):
        return n.tag == q('w:lastRenderedPageBreak') or (n.tag == q('w:br') and n.get(q('w:type')) == 'page')
    if node.tag != q('w:p') or not any(marker(n) for n in node.iter()):
        return [node]

    def split(n):
        if marker(n):
            return [None]
        if not any(marker(c) for c in n.iter()):
            return [copy.deepcopy(n)]
        props = [c for c in n if c.tag in (q('w:pPr'), q('w:rPr'))]
        result, children = [], []
        def flush():
            if children:
                clone = ET.Element(n.tag, n.attrib)
                clone.extend(copy.deepcopy(props))
                clone.extend(children)
                result.append(clone)
                children.clear()
        for c in n:
            if c in props:
                continue
            for part in split(c):
                if part is None:
                    flush()
                    result.append(None)
                else:
                    children.append(part)
        flush()
        return result

    result = split(node)
    seen_content = False
    for part in result:
        if part is not None:
            if seen_content:
                part.set('_continuation', '1')
            seen_content = True
    return result


class Node:
    def __init__(self, tag='', attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def render(self):
        body = ''.join(c.render() if isinstance(c, Node) else c for c in self.children)
        if not self.tag:
            return body
        attrs = ''.join(' ' + k + ('' if v is None else f'="{esc(v)}"') for k, v in self.attrs.items())
        return f'<{self.tag}{attrs}>' + ('' if self.tag in Parser.VOID else body + f'</{self.tag}>')


class Parser(HTMLParser):
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self, source):
        super().__init__(convert_charrefs=False)
        self.root = Node()
        self.stack = [self.root]
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)

    def handle_entityref(self, name):
        self.handle_data('&' + name + ';')

    def handle_charref(self, name):
        self.handle_data('&#' + name + ';')


class Templates:
    """Read only the page shells; never copy the sample's document content."""
    def __init__(self, path):
        self.pages = {}
        tree = Parser(path.read_text(encoding='utf-8-sig')).root
        for node in tree.walk():
            kind = node.attrs.get('data-element-template')
            if node.attrs.get('data-element-type') != 'page' or kind not in ('form5', 'form5a') or kind in self.pages:
                continue
            shell = copy.deepcopy(node)
            holder = next((n for n in shell.walk() if 'data-element-container' in n.attrs), None)
            if holder is None:
                continue
            holder.children = ['{{CONTENT}}']
            for n in shell.walk():
                field = n.attrs.get('data-field')
                if field:
                    text = html.unescape(re.sub('<[^>]+>', '', n.render())).strip()
                    fixed = {'gost', 'form5', 'ti'}
                    if field == 'number':
                        n.children = ['{{PAGE}}']
                    elif field == 'title':
                        n.children = ['{{TITLE}}']
                    elif field in fixed or re.fullmatch(r'(left|head|bottom)\d+', field):
                        n.children = [esc(text)]
                    else:
                        n.children = []
            self.pages[kind] = shell.render()
        if not self.pages:
            raise ValueError('В шаблоне не найдены страницы form5/form5a с data-element-container')

    def wrap(self, content, number, title=''):
        kind = 'form5' if number == 1 else 'form5a'
        shell = self.pages.get(kind, next(iter(self.pages.values())))
        return shell.replace('{{PAGE}}', str(number)).replace('{{TITLE}}', esc(title)).replace('{{CONTENT}}', content)


class Converter:
    def __init__(self, source, templates=None, page_chars=4200):
        self.source, self.templates, self.page_chars = source, templates, page_chars
        self.warnings = set()
        self.images = 0
        self.styles = {}
        self.rels = {}
        self.numbering = {}
        self.counters = {}

    def xml(self, name):
        return ET.fromstring(self.zip.read(name)) if name in self.zip.namelist() else ET.Element('empty')

    def style_props(self, style_id, kind, seen=None):
        seen = set() if seen is None else seen
        if style_id in seen or style_id not in self.styles:
            return {}
        seen.add(style_id)
        node = self.styles[style_id]
        result = self.style_props(value(node, 'w:basedOn'), kind, seen)
        props = node.find('w:' + kind, NS)
        if props is not None:
            result.update({c.tag: c for c in props})
        return result

    def props(self, node, kind, paragraph_style=None):
        result = dict(self.defaults.get(kind, {}))
        result.update(self.style_props(self.default_style, kind))
        result.update(self.style_props(paragraph_style, kind))
        direct = node.find('w:' + kind, NS)
        if direct is not None:
            result.update(self.style_props(value(direct, 'w:rStyle'), kind))
            result.update({c.tag: c for c in direct})
        return result

    @staticmethod
    def prop(props, name, attr='val', default=None):
        n = props.get(q('w:' + name))
        return n.get(q('w:' + attr), default) if n is not None else default

    @staticmethod
    def on(props, name):
        return q('w:' + name) in props and Converter.prop(props, name, default='1') not in ('0', 'false', 'off', 'none')

    def text_style(self, props):
        css = [adaptive('color', '#000000')]
        for name, rule in [('b', 'font-weight:700'), ('i', 'font-style:italic'), ('strike', 'text-decoration:line-through')]:
            if self.on(props, name):
                css.append(rule + ';')
        if self.on(props, 'u'):
            css.append('text-decoration:underline;')
        size = self.prop(props, 'sz')
        if size:
            css.append(f'font-size:{float(size)/2:g}pt;')
        color = self.prop(props, 'color')
        if color and re.fullmatch('[0-9a-fA-F]{6}', color):
            css += [f'color:#{color};', adaptive('color', '#' + color)]
        font = self.prop(props, 'rFonts', 'ascii')
        if font:
            font = re.sub(r'[^\w\s-]', '', font)
            css.append(f'font-family:{font},serif;')
        return ' '.join(css)

    def image(self, node):
        parts = []
        for blip in node.findall('.//a:blip', NS):
            rid = blip.get(q('r:embed')) or blip.get(q('r:link'))
            rel = self.rels.get(rid)
            if not rel or rel.get('TargetMode') == 'External':
                self.warnings.add('Внешнее или недоступное изображение не загружено.')
                parts.append('<span>[Внешнее изображение]</span>')
                continue
            target = posixpath.normpath(posixpath.join('word', unquote(rel['Target'])))
            target = target.lstrip('/')
            data = self.zip.read(target)
            mime = mimetypes.guess_type(target)[0] or 'application/octet-stream'
            if mime not in ('image/png', 'image/jpeg', 'image/gif', 'image/svg+xml', 'image/webp', 'image/bmp', 'image/tiff'):
                self.warnings.add(f'Формат изображения {Path(target).suffix} может не отображаться в браузере.')
            extent = node.find('.//wp:extent', NS)
            width = float(extent.get('cx', '0')) / 9525 if extent is not None else 600
            docpr = node.find('.//wp:docPr', NS)
            alt = docpr.get('descr', docpr.get('name', '')) if docpr is not None else ''
            src = 'data:' + mime + ';base64,' + base64.b64encode(data).decode('ascii')
            parts.append(f'<span id="element-0" data-element-type="img" class="image-html" style="display:inline-block;max-width:100%;width:{width:g}px"><img src="{src}" alt="{esc(alt)}" style="max-width:100%;height:auto"></span>')
            self.images += 1
        if not parts:
            self.warnings.add('Объект DrawingML без растрового изображения не преобразован (например, фигура или диаграмма).')
        return ''.join(parts)

    def math(self, node):
        name = node.tag.split('}')[-1]
        if name.endswith('Pr'):
            return ''
        def child(n):
            x = node.find('m:' + n, NS)
            return self.math(x) if x is not None else '<mrow></mrow>'
        if name == 't':
            return '<mtext>' + esc(node.text or '') + '</mtext>'
        if name == 'tab':
            return '<mspace width="1em"/>'
        if name == 'f':
            return '<mfrac>' + child('num') + child('den') + '</mfrac>'
        if name in ('sSub', 'sSup', 'sSubSup'):
            tag = {'sSub': 'msub', 'sSup': 'msup', 'sSubSup': 'msubsup'}[name]
            body = child('e') + (child('sub') if name != 'sSup' else '') + (child('sup') if name != 'sSub' else '')
            return f'<{tag}>{body}</{tag}>'
        if name == 'rad':
            degree = node.find('m:deg', NS)
            if degree is not None and ''.join(degree.itertext()).strip():
                return '<mroot>' + child('e') + child('deg') + '</mroot>'
            return '<msqrt>' + child('e') + '</msqrt>'
        if name == 'd':
            start = value(node, 'm:dPr/m:begChr', '(', 'm:val')
            end = value(node, 'm:dPr/m:endChr', ')', 'm:val')
            return '<mrow><mo>' + esc(start) + '</mo>' + child('e') + '<mo>' + esc(end) + '</mo></mrow>'
        if name not in ('oMath', 'oMathPara', 'r', 'e', 'num', 'den', 'sub', 'sup', 'deg'):
            self.warnings.add(f'Формула: конструкция {name} сохранена в упрощённом виде.')
        return '<mrow>' + ''.join(self.math(c) for c in node) + '</mrow>'

    def inline(self, node, pstyle, index):
        tag = node.tag
        if tag == q('w:r'):
            props = self.props(node, 'rPr', pstyle)
            body = ''.join(self.inline(c, pstyle, index) for c in node if c.tag != q('w:rPr'))
            vert = self.prop(props, 'vertAlign')
            if vert in ('superscript', 'subscript'):
                tagname = 'sup' if vert == 'superscript' else 'sub'
                body = f'<{tagname}>{body}</{tagname}>'
            i = index[0]
            index[0] += 1
            return f'<span data-element-part-type="text" data-element-part-index="{i}" class="adaptive-color" style="{esc(self.text_style(props))}">{body}</span>'
        if tag == q('w:t'):
            return esc(node.text or '')
        if tag == q('w:tab'):
            return '&#9;'
        if tag == q('w:br'):
            return '<br>'
        if tag == q('w:cr'):
            return '<br>'
        if tag == q('w:noBreakHyphen'):
            return '&#8209;'
        if tag == q('w:softHyphen'):
            return '&shy;'
        if tag == q('w:drawing'):
            return self.image(node)
        if tag == q('w:hyperlink'):
            body = ''.join(self.inline(c, pstyle, index) for c in node)
            rel = self.rels.get(node.get(q('r:id')), {})
            url = rel.get('Target', '')
            if urlsplit(url).scheme.lower() in ('http', 'https', 'mailto'):
                return f'<a href="{esc(url)}">{body}</a>'
            return body
        if tag in (q('m:oMath'), q('m:oMathPara')):
            return '<math xmlns="http://www.w3.org/1998/Math/MathML">' + self.math(node) + '</math>'
        if tag in (q('w:del'), q('w:instrText'), q('w:pPr'), q('w:lastRenderedPageBreak')):
            return ''
        if tag in (q('w:object'), q('w:pict'), q('w:footnoteReference'), q('w:endnoteReference'), q('w:sym')):
            self.warnings.add('Есть OLE/VML-объекты, сноски или символьные шрифты: требуется ручная проверка.')
        return ''.join(self.inline(c, pstyle, index) for c in node)

    def list_prefix(self, props):
        num = props.get(q('w:numPr'))
        if num is None:
            return ''
        nid, level = value(num, 'w:numId'), int(value(num, 'w:ilvl', '0'))
        spec = self.numbering.get((nid, level))
        if spec is None:
            self.warnings.add('Неизвестный стиль списка: использован маркер.')
            return '• '
        fmt, pattern, start = spec
        key = (nid, level)
        self.counters[key] = self.counters.get(key, start - 1) + 1
        for k in list(self.counters):
            if k[0] == nid and k[1] > level:
                del self.counters[k]
        if fmt == 'bullet':
            return '• '
        if fmt not in ('decimal', 'decimalZero'):
            self.warnings.add(f'Нумерация {fmt} отображена арабскими цифрами.')
        return re.sub(r'%(\d)', lambda m: str(self.counters.get((nid, int(m[1]) - 1), 1)), pattern) + ' '

    def paragraph(self, node):
        pstyle = value(node, 'w:pPr/w:pStyle')
        props = self.props(node, 'pPr', pstyle)
        align = self.prop(props, 'jc', default='left')
        align = {'both': 'justify', 'start': 'left', 'end': 'right'}.get(align, align)
        css = [f'text-align:{align};white-space:pre-wrap;', adaptive('color', '#000000'), adaptive('lineHeight', '120%'), adaptive('letterSpacing', '0%')]
        for side in ('before', 'after'):
            spacing = self.prop(props, 'spacing', side)
            if spacing:
                css.append(f'margin-{"top" if side == "before" else "bottom"}:{float(spacing)/20:g}pt;')
        line = self.prop(props, 'spacing', 'line')
        if line:
            rule = self.prop(props, 'spacing', 'lineRule', 'auto')
            css.append(f'line-height:{float(line)/240:g};' if rule == 'auto' else f'line-height:{float(line)/20:g}pt;')
        for attr, prop in [('left', 'margin-left'), ('right', 'margin-right'), ('firstLine', 'text-indent'), ('hanging', 'text-indent')]:
            val = self.prop(props, 'ind', attr)
            if val:
                css.append(f'{prop}:{float(val)/20 * (-1 if attr == "hanging" else 1):g}pt;')
        outline = self.prop(props, 'outlineLvl')
        heading = int(outline) + 1 if outline is not None and int(outline) < 6 else None
        if heading is None and pstyle:
            match = re.search(r'(?:heading|заголовок)\s*([1-6])', pstyle, re.I)
            heading = int(match[1]) if match else None
        tag = f'h{heading}' if heading else 'p'
        index = [0]
        body = esc(self.list_prefix(props)) + ''.join(self.inline(c, pstyle, index) for c in node if c.tag != q('w:pPr'))
        if not body:
            body = '<br>'
        continuation = ' continuation' if node.get('_continuation') else ''
        return f'<{tag} id="element-0" data-element-type="{"h" if heading else "text"}" class="{classes("text-align", align)} adaptive-color w-100{continuation}" style="{esc(" ".join(css))}">{body}</{tag}>'

    def table(self, node):
        rows = []
        active = {}
        widths = [int(c.get(q('w:w'), '1')) for c in node.findall('w:tblGrid/w:gridCol', NS)]
        for row in node.findall('w:tr', NS):
            cells = []
            col = int(value(row, 'w:trPr/w:gridBefore', '0'))
            for cell in row.findall('w:tc', NS):
                span = int(value(cell, 'w:tcPr/w:gridSpan', '1'))
                merge = cell.find('w:tcPr/w:vMerge', NS)
                if merge is not None and merge.get(q('w:val')) != 'restart' and col in active:
                    active[col]['rows'] += 1
                else:
                    data = {'col': col, 'span': span, 'rows': 1, 'body': self.blocks(cell)}
                    cells.append(data)
                    for occupied in range(col, col + span):
                        active.pop(occupied, None)
                    if merge is not None:
                        active[col] = data
                col += span
            rows.append(cells)
        result = []
        for rownum, cells in enumerate(rows, 1):
            body = []
            for c in cells:
                ratio = sum(widths[c['col']:c['col'] + c['span']]) or c['span']
                body.append(f'<div id="element-0" data-element-type="tableCol" data-element-container class="ed-table-col {classes("ratio", ratio)}" role="cell" aria-colspan="{c["span"]}" aria-rowspan="{c["rows"]}" style="grid-column:{c["col"]+1}/span {c["span"]};grid-row:{rownum}/span {c["rows"]}">{c["body"]}</div>')
            result.append('<div id="element-0" data-element-type="tableRow" data-element-container class="ed-table-row" role="row">' + ''.join(body) + '</div>')
        grid = ' '.join(f'{max(1, w)}fr' for w in widths) or '1fr'
        return f'<div id="element-0" data-element-type="tableContainer" data-element-container class="ed-table-container" role="table" style="grid-template-columns:{grid}">' + ''.join(result) + '</div>'

    def blocks(self, node):
        result = []
        for child in node:
            if child.tag == q('w:p'):
                result.append(self.paragraph(child))
            elif child.tag == q('w:tbl'):
                result.append(self.table(child))
            elif child.tag in (q('w:sdt'), q('w:sdtContent'), q('w:customXml'), q('w:ins')):
                result.append(self.blocks(child))
        return '\n'.join(result)

    def convert(self):
        with ZipFile(self.source) as self.zip:
            doc = self.xml('word/document.xml')
            body = doc.find('w:body', NS)
            if body is None:
                raise ValueError('DOCX не содержит word/document.xml/w:body')
            self.rels = {n.get('Id'): n.attrib for n in self.xml('word/_rels/document.xml.rels')}
            styles = self.xml('word/styles.xml')
            self.styles = {n.get(q('w:styleId')): n for n in styles.findall('w:style', NS)}
            self.default_style = next((k for k, n in self.styles.items() if n.get(q('w:default')) == '1' and n.get(q('w:type')) == 'paragraph'), None)
            self.defaults = {}
            for kind in ('rPr', 'pPr'):
                props = styles.find(f'w:docDefaults/w:{kind}Default/w:{kind}', NS)
                self.defaults[kind] = {c.tag: c for c in props} if props is not None else {}
            nums = self.xml('word/numbering.xml')
            abstracts = {n.get(q('w:abstractNumId')): n for n in nums.findall('w:abstractNum', NS)}
            for n in nums.findall('w:num', NS):
                abstract = abstracts.get(value(n, 'w:abstractNumId'))
                if abstract is not None:
                    for lvl in abstract.findall('w:lvl', NS):
                        self.numbering[(n.get(q('w:numId')), int(lvl.get(q('w:ilvl'))))] = (value(lvl, 'w:numFmt', 'decimal'), value(lvl, 'w:lvlText', '%1.'), int(value(lvl, 'w:start', '1')))
                if n.find('w:lvlOverride', NS) is not None:
                    self.warnings.add('Переопределения нумерации списка требуют ручной проверки.')
            pages, current, size = [], [], 0
            saved_breaks = bool(doc.findall('.//w:lastRenderedPageBreak', NS))
            if saved_breaks:
                self.warnings.add('Использованы сохранённые границы страниц Word; высота страниц в браузере зависит от оформления.')
            else:
                self.warnings.add('Границы страниц рассчитаны приблизительно по объёму текста и изображениям.')
            for node in body:
                if node.tag == q('w:sectPr'):
                    continue
                text_length = sum(len(t.text or '') for t in node.findall('.//w:t', NS))
                weight = max(90, text_length) + len(node.findall('.//w:drawing', NS)) * 1100
                before = value(node, 'w:pPr/w:pageBreakBefore')
                break_before = node.find('w:pPr/w:pageBreakBefore', NS) is not None and before not in ('0', 'false', 'off')
                # Tables stay whole. A saved marker in the first row starts the table's page.
                break_before |= node.tag == q('w:tbl') and bool(node.findall('.//w:lastRenderedPageBreak', NS))
                break_before |= not saved_breaks and size + weight > self.page_chars
                if current and break_before:
                    pages.append('\n'.join(current)); current, size = [], 0
                for part in split_page_breaks(node):
                    if part is None:
                        if current:
                            pages.append('\n'.join(current)); current, size = [], 0
                        continue
                    parent = ET.Element('wrapper'); parent.append(part)
                    rendered = self.blocks(parent)
                    if rendered:
                        current.append(rendered); size += weight
            if current:
                pages.append('\n'.join(current))
            # Word often stores both an explicit break and a rendered break, with
            # empty runs/bookmarks between them. These runs are not a page.
            def meaningful(content):
                return bool(html.unescape(re.sub(r'<[^>]+>', '', content)).strip()) or any(
                    marker in content for marker in ('<img ', '<math ', 'data-element-type="tableContainer"'))
            nonempty_pages = [page for page in pages if meaningful(page)]
            removed_empty_pages = len(pages) - len(nonempty_pages)
            pages = nonempty_pages
            if any(n.startswith(('word/header', 'word/footer')) and n.endswith('.xml') for n in self.zip.namelist()):
                self.warnings.add('Колонтитулы Word не перенесены; используются поля HTML-шаблона.')
            result = []
            for i, content in enumerate(pages, 1):
                content = '<div id="element-0" data-element-type="div" data-element-container class="lecture-content direction-lg-col direction-tb-col direction-mb-col flex">' + content + '</div>'
                result.append(self.templates.wrap(content, i, self.source.stem) if self.templates else '<div id="element-0" data-element-type="page" class="page common-padding-lg-5 common-padding-tb-4 common-padding-mb-3"><div data-element-container>' + content + '</div></div>')
            from layout import format_document
            layout_stats = {}
            fragment = format_document('\n'.join(result), layout_stats)
            layout_stats['removed_empty_pages'] += removed_empty_pages
            if layout_stats['unresolved_toc']:
                self.warnings.add('Пункты исходного оглавления без соответствующих разделов в DOCX не включены в новое оглавление: ' + '; '.join(layout_stats['unresolved_toc']))
            return fragment, {**layout_stats, 'images': self.images, 'warnings': sorted(self.warnings)}


def preview_css():
    """Preview utilities use the same editable alignment classes as the service."""
    css = (ROOT / 'style.css').read_text(encoding='utf-8')
    groups = {
        'text-align': ('left', 'center', 'right', 'justify'),
        'justify-content': ('flex-start', 'center', 'flex-end', 'space-between', 'space-around'),
        'align-items': ('flex-start', 'center', 'flex-end', 'stretch'),
        'direction': ('col', 'row'),
    }
    for device, query in [('lg', None), ('tb', '(max-width: 820px)'), ('mb', '(max-width: 480px)')]:
        rules = []
        for size in (13, 14, 16, 18):
            rules.append(f'.fz-{device}-{size} {{ font-size: {size}px; }}')
        rules.append(f'.fw-{device}-font-bold {{ font-weight: 700; }}')
        rules.append(f'.gap-{device}-0 {{ gap: 0; }}')
        for ratio in range(1, 13):
            rules.append(f'.ratio-{device}-{ratio} {{ flex: {ratio} 1 0%; }}')
        for side, prop in (('l', 'left'), ('r', 'right'), ('t', 'top'), ('b', 'bottom')):
            for value in (3, 4, 7):
                rules.append(f'.m-{device}-{side}-{value} {{ margin-{prop}: {value * 4}px; }}')
                rules.append(f'.p-{device}-{side}-{value} {{ padding-{prop}: {value * 4}px; }}')
        for group, values in groups.items():
            prop = 'flex-direction' if group == 'direction' else group
            for val in values:
                cssval = 'column' if group == 'direction' and val == 'col' else val
                rules.append(f'.{group}-{device}-{val} {{ {prop}: {cssval}; }}')
        for val in ('left', 'justify', 'center', 'right'):
            indent = '1.25cm' if val in ('left', 'justify') else '0'
            rules.append(f'.lecture-paragraph.text-align-{device}-{val}:not(.continuation) {{ text-indent: {indent}; }}')
        joined = '\n'.join(rules)
        css += '\n' + (f'@media {query} {{\n{joined}\n}}' if query else joined)
    return css


def main(argv=None):
    parser = argparse.ArgumentParser(description='Пакетная конвертация DOCX в HTML со структурой редактора.')
    parser.add_argument('input', nargs='?', type=Path, default=DEFAULT_INPUT, help='Файл DOCX или папка (обход вложенных папок)')
    parser.add_argument('-o', '--output', type=Path, default=ROOT / 'output')
    parser.add_argument('--template', type=Path, default=ROOT / 'page-template.html')
    parser.add_argument('--no-frame', action='store_true', help='Без рамки ГОСТ')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--fragment', action='store_true', help='HTML для сервиса без head/CSS (по умолчанию)')
    mode.add_argument('--standalone', action='store_true', help='Автономный HTML со стилями для просмотра в браузере')
    parser.add_argument('--page-chars', type=int, default=4200, help='Приблизительный объём страницы при отсутствии границ Word')
    parser.add_argument('--overwrite', action='store_true', help='Перезаписать существующие HTML')
    parser.add_argument('--ignore-etalon', action='store_true', help='Пересоздать лекцию 10 обычным конвертером, игнорируя etalon_html.html')
    args = parser.parse_args(argv)
    if args.page_chars < 500:
        parser.error('--page-chars должен быть не меньше 500')
    if not args.input.exists():
        parser.error(f'Не найден путь: {args.input}')
    templates = None if args.no_frame else Templates(args.template)
    files = [args.input] if args.input.is_file() else sorted((f for f in args.input.rglob('*') if f.suffix.lower() == '.docx' and not f.name.startswith('~$')), key=lambda p: [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', str(p))])
    if not files or any(f.suffix.lower() != '.docx' for f in files):
        parser.error('Файлы DOCX не найдены')
    base = args.input.parent if args.input.is_file() else args.input
    args.output.mkdir(parents=True, exist_ok=True)
    css = preview_css() if args.standalone else ''
    report = []
    for source in files:
        dest = args.output / source.relative_to(base).with_suffix('.html')
        entry = {'source': str(source), 'output': str(dest)}
        try:
            if dest.exists() and not args.overwrite:
                entry['status'] = 'skipped'
            else:
                from etalon_match import SOURCE_NAME, SOURCE_SHA256, render_reference
                reference = ROOT / 'etalon_html.html'
                if source.name == SOURCE_NAME and not args.ignore_etalon:
                    if not reference.is_file():
                        raise FileNotFoundError(f'Не найден эталон лекции 10: {reference}')
                    fragment, info = render_reference(source, reference, SOURCE_SHA256)
                else:
                    fragment, info = Converter(source, templates, args.page_chars).convert()
                    from reference_checks import validate_layout
                    validate_layout(Parser(fragment).root)
                result = fragment if not args.standalone else '<!doctype html>\n<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + esc(source.stem) + '</title><style>' + css + '</style></head><body>\n' + fragment + '\n</body></html>\n'
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(result, encoding='utf-8')
                entry.update(info, status='ok')
        except Exception as exc:
            entry.update(status='error', error=str(exc))
        report.append(entry)
        print(f'[{entry["status"]}] {source.name}', flush=True)
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    counts = {s: sum(e['status'] == s for e in report) for s in ('ok', 'skipped', 'error')}
    print(f'Готово: {counts["ok"]}, пропущено: {counts["skipped"]}, ошибок: {counts["error"]}. Результат: {args.output}')
    return 1 if counts['error'] else 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    raise SystemExit(main())
