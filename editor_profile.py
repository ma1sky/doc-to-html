"""Native editor blocks from the user's third lecture-1 reference."""
import html
import re
import json
from pathlib import Path

from convert import Node, classes, adaptive


def apply_profile(tree):
    from layout import make, text, style_values
    color = adaptive('color', '#000000')
    paragraph_style = color + ' ' + adaptive('lineHeight', '120%') + ' ' + adaptive('letterSpacing', '0%')
    flex = (classes('direction', 'col') + ' ' + classes('justify-content', 'flex-start') + ' '
            + classes('align-items', 'center') + ' ' + classes('gap', 0) + ' flex')
    margin_left = ' '.join(f'm-{d}-l-7' for d in ('lg', 'tb', 'mb'))
    title_margin = ' '.join(f'm-{d}-{s}-3' for s in ('t', 'b') for d in ('lg', 'tb', 'mb'))
    containers = [n for n in tree.walk() if 'lecture-content' in n.attrs.get('class', '').split()]
    table_layouts = json.loads(Path(__file__).with_name('editor-table-layouts.json').read_text(encoding='utf-8'))
    for container in containers:
        cover = 'cover-content' in container.attrs.get('class', '').split()
        container.children = [n for n in container.children if not isinstance(n, Node) or n.attrs.get('data-element-type') != 'divEmpty']
        if cover:
            titles = [n for n in container.children if isinstance(n, Node) and 'cover-title' in n.attrs.get('class', '').split()]
            if titles:
                group = make('div', {'id': 'element-0', 'data-element-type': 'div', 'data-element-container': '', 'class': flex}, titles)
                first = container.children.index(titles[0])
                container.children = [n for n in container.children if n not in titles]
                container.children.insert(first, group)
            container.attrs['class'] = flex + ' ' + ' '.join(f'p-{d}-{s}-7' for s in ('l', 'r') for d in ('lg', 'tb', 'mb'))
        else:
            # Ordinary pages expose their blocks directly to the editor's frame slot.
            parent = next(n for n in tree.walk() if container in n.children)
            pos = parent.children.index(container)
            parent.children[pos:pos + 1] = container.children
    for n in list(tree.walk()):
        kind = n.attrs.get('data-element-type')
        old = n.attrs.get('class', '').split()
        if kind in ('text', 'h'):
            cover = 'cover-text' in old
            cell = 'cell-text' in old
            size = 18 if kind == 'h' else 14 if cover else 13 if cell else 16
            match = re.search(r'\btext-align-lg-(\w+)', n.attrs.get('class', ''))
            align = match[1] if match else 'left'
            if align == 'justify':
                align = 'left'
            n.attrs['class'] = classes('fz', size) + ' ' + classes('text-align', align) + ' adaptive-color w-100'
            n.attrs['style'] = paragraph_style
            if not cover and not cell:
                n.attrs['class'] += ' ' + margin_left
            if kind == 'h':
                n.attrs['class'] += ' ' + classes('fw', 'font-bold') + ' ' + title_margin
                n.children = [html.escape(text(n))]
            else:
                for span in n.walk():
                    if span.attrs.get('data-element-part-type') == 'text':
                        props = style_values(span.attrs.get('style', ''))
                        retained = {k: v for k, v in props.items() if k in ('font-weight', 'font-style', 'text-decoration', 'background-color')}
                        span.attrs['class'] = classes('fz', size) + ' adaptive-color'
                        if retained.get('font-weight') in ('700', 'bold'):
                            span.attrs['data-element-part-type'] = 'span'
                            span.attrs['class'] += ' ' + classes('fw', 'font-bold')
                            retained.pop('font-weight')
                        span.attrs['style'] = color + ' ' + ' '.join(f'{k}:{v};' for k, v in retained.items())
        elif kind == 'img':
            n.attrs['class'] = 'image-html flex ' + classes('justify-content', 'center') + ' ' + ' '.join(f'm-{d}-t-4' for d in ('lg', 'tb', 'mb'))
            for inner in n.walk():
                if 'adaptive-width' in inner.attrs.get('class', '').split():
                    width = style_values(inner.attrs.get('style', ''))['--width-lg']
                    inner.attrs['style'] = adaptive('width', width)
                if 'image-label' in inner.attrs.get('class', '').split():
                    inner.attrs['style'] = color
                    inner.children = [html.escape(text(inner))]
        elif kind == 'toc':
            for row in n.walk():
                if 'toc-row' in row.attrs.get('class', '').split():
                    row.tag = 'div'
                    row.attrs = {'class': 'toc-row level-0'}
        elif kind == 'tableContainer':
            merged = any(int(c.attrs.get('aria-colspan', '1')) > 1 or int(c.attrs.get('aria-rowspan', '1')) > 1 for c in n.walk())
            rows = [r for r in n.children if isinstance(r, Node) and r.attrs.get('data-element-type') == 'tableRow']
            first_cells = [c for c in rows[0].children if isinstance(c, Node)] if rows else []
            header = '|'.join(text(c) for c in first_cells)
            ratios = table_layouts.get(header)
            if ratios is None:
                widths = [int(re.search(r'ratio-lg-(\d+)', c.attrs.get('class', ''))[1]) for c in first_cells]
                ratios = [max(1, round(w / max(widths) * 3)) for w in widths] if widths else []
            if not merged:
                for row in rows:
                    for col, cell in enumerate(c for c in row.children if isinstance(c, Node)):
                        cell.attrs['class'] = classes('ratio', ratios[col] if col < len(ratios) else 3) + ' ed-table-col'
            for block in n.walk():
                if block.attrs.get('data-element-type') in ('tableContainer', 'tableRow', 'tableCol'):
                    native = adaptive('border-color', '#909399') + ' ' + adaptive('bg-color', '#ffffff')
                    if merged:
                        block.attrs['style'] = block.attrs.get('style', '') + ';' + native
                    else:
                        for key in ('role', 'aria-colspan', 'aria-rowspan'):
                            block.attrs.pop(key, None)
                        block.attrs['style'] = native
