"""Apply the lecture editor layout from the supplied reference HTML."""
import copy
import html
import re

from convert import Node, Parser, adaptive, classes


def text(node):
    return html.unescape(re.sub(r'<[^>]+>', '', node.render())).strip()


def style_values(style):
    return dict((k.strip(), v.strip()) for part in style.split(';') if ':' in part for k, v in [part.split(':', 1)])


def base_style():
    return ' '.join(adaptive(k, v) for k, v in [('color', '#000000'), ('lineHeight', '140%'), ('letterSpacing', '0%')])


def make(tag, attrs=None, children=None):
    n = Node(tag, (attrs or {}).items())
    n.children = children or []
    return n


def lift_images(node):
    """Split inline ancestors around images so a block image never lives in a p/span."""
    if isinstance(node, str):
        return [node]
    if node.attrs.get('data-element-type') == 'img':
        return [node]
    result, group = [], []
    def flush():
        if group:
            clone = make(node.tag, dict(node.attrs), list(group))
            result.append(clone)
            group.clear()
    for child in node.children:
        for part in lift_images(child):
            if isinstance(part, Node) and part.attrs.get('data-element-type') == 'img':
                flush()
                result.append(part)
            else:
                group.append(part)
    flush()
    if not result:
        result.append(node)
    return result


def normalize_paragraph(node, cover=False, cell=False):
    plain = text(node)
    old_class = node.attrs.get('class', '')
    if not plain and not any(n.tag in ('math', 'img') for n in node.walk()):
        if cover:
            return make('div', {'id': 'element-0', 'data-element-type': 'divEmpty', 'class': 'cover-space',
                                'style': adaptive('minHeight', '1%')})
        return None
    styles = style_values(node.attrs.get('style', ''))
    align_class = re.search(r'\btext-align-lg-(left|center|right|justify)\b', old_class)
    align = styles.get('text-align', align_class[1] if align_class else 'left')
    is_heading = node.attrs.get('data-element-type') == 'h'
    bold = any('font-weight:700' in n.attrs.get('style', '') for n in node.walk())
    if cover and align == 'center' and bold:
        node.tag = 'h5'
        node.attrs['data-element-type'] = 'h'
        semantic = 'cover-text cover-title'
    elif cover:
        semantic = 'cover-text'
    elif cell:
        semantic = 'cell-text'
    elif is_heading:
        level = int(node.tag[1]) if re.fullmatch(r'h[1-6]', node.tag) else 1
        node.tag = 'h3' if level == 1 else 'h5'
        semantic = 'lecture-heading'
    elif re.match(r'^Таблица\s+\d', plain, re.I):
        semantic = 'table-caption'
    else:
        semantic = 'lecture-paragraph'
    if 'continuation' in old_class:
        semantic += ' continuation'
    node.attrs['class'] = f'{classes("fz", 13 if cell else 18)} {classes("text-align", align)} adaptive-color w-100 {semantic}'
    node.attrs['style'] = base_style()
    for n in node.walk():
        if n.attrs.get('data-element-part-type') == 'text':
            props = style_values(n.attrs.get('style', ''))
            retained = {k: v for k, v in props.items() if k in ('font-weight', 'font-style', 'text-decoration', 'color', 'background-color')}
            n.attrs['style'] = base_style() + ' ' + ' '.join(f'{k}:{v};' for k, v in retained.items())
    return node


def normalize_image(node, caption=None):
    image = next((n for n in node.walk() if n.tag == 'img'), None)
    if image is None:
        return node
    width = style_values(node.attrs.get('style', '')).get('width', '660px')
    match = re.fullmatch(r'([\d.]+)px', width)
    percent = min(100, float(match[1]) / 660 * 100) if match else 100
    node.tag = 'div'
    node.attrs = {'id': node.attrs.get('id', 'element-0'), 'data-element-type': 'img',
                  'class': 'image-html flex flex-1 w-100 ' + classes('justify-content', 'center')}
    image.attrs.update({'class': 'adaptive-aspect-ratio', 'style': 'width: 100%; height: auto'})
    inner = make('div', {'class': 'flex flex-col items-center adaptive-width',
                         'style': f'--width-lg: {percent:.14g}%; --width-tb: 100%; --width-mb: 100%;'}, [image])
    if caption is not None:
        image.attrs['alt'] = text(caption)
        normalize_paragraph(caption)
        label = make('div', {'class': 'image-label ' + classes('fz', 16) + ' adaptive-color', 'style': base_style()}, caption.children)
        inner.children.append(label)
    node.children = [inner]
    return node


def normalize_container(container, cover=False, cell=False):
    expanded = []
    for child in container.children:
        if isinstance(child, Node) and child.attrs.get('data-element-type') in ('text', 'h'):
            expanded.extend(lift_images(child))
        else:
            expanded.append(child)
    elements = [n for n in expanded if isinstance(n, Node)]
    result, consumed = [], set()
    for index, child in enumerate(elements):
        if index in consumed:
            continue
        kind = child.attrs.get('data-element-type')
        if kind == 'img':
            caption = None
            if index + 1 < len(elements):
                candidate = elements[index + 1]
                if candidate.attrs.get('data-element-type') == 'text' and re.match(r'^(?:Рисунок|Рис\.)\s*\d', text(candidate), re.I):
                    caption = candidate
                    consumed.add(index + 1)
            result.append(normalize_image(child, caption))
        elif kind in ('text', 'h'):
            normalized = normalize_paragraph(child, cover, cell)
            if normalized is not None:
                result.append(normalized)
        else:
            for n in child.walk():
                if n.attrs.get('data-element-type') == 'tableCol':
                    normalize_container(n, cell=True)
                    n.attrs['class'] = classes('ratio', 3) + ' ed-table-col'
            result.append(child)
    container.children = result


def content_present(node):
    """Page decorations and spacer blocks alone are not document content."""
    return bool(text(node)) or any(n.tag in ('img', 'math') or n.attrs.get('data-element-type') == 'tableContainer' for n in node.walk())


def heading_key(title):
    return re.sub(r'[^\w]', '', title.casefold().replace('ё', 'е'))


def section_number(title):
    match = re.match(r'^(\d+(?:\.\d+)*)(?:\s|[.)]\s)', title)
    return match[1] if match else None


def format_document(fragment, stats=None):
    tree = Parser(fragment).root
    containers = [n for n in tree.walk() if 'lecture-content' in n.attrs.get('class', '').split()]
    for index, container in enumerate(containers):
        # A title page is identified by its institutional heading, not just page number.
        cover = index == 0 and bool(re.search(r'Минобр|Министерств|университет|институт', text(container), re.I))
        container.attrs['class'] = (' '.join(f'p-{device}-{side}-7' for device in ('lg', 'tb', 'mb') for side in ('l', 'r'))
            + ' ' + classes('direction', 'col') + ' ' + classes('justify-content', 'flex-start')
            + ' ' + classes('align-items', 'center') + ' ' + classes('gap', 0) + ' flex lecture-content' + (' cover-content' if cover else ''))
        normalize_container(container, cover)
    # Read the entire source TOC, including continuations on later pages.
    toc_title, toc_container = None, None
    source_rows, reading = [], False
    removable = set()
    for container in containers:
        for node in container.children:
            title = text(node)
            if title.casefold() in ('содержание', 'оглавление'):
                if toc_title is None:
                    toc_title, toc_container = node, container
                    reading = True
            elif reading and node.attrs.get('data-element-type') == 'h':
                reading = False
            elif reading and node.attrs.get('data-element-type') == 'text':
                if 'continuation' in node.attrs.get('class', '') and source_rows:
                    source_rows[-1][0] += title
                    source_rows[-1][1].append(node)
                else:
                    source_rows.append([title, [node]])
    targets = [n for n in tree.walk() if n.attrs.get('data-element-type') == 'h'
               and 'cover-title' not in n.attrs.get('class', '') and n is not toc_title]
    if toc_title is not None:
        candidates = [toc_title] + targets
    else:
        candidates = targets
    exact, numbered = {}, {}
    for n in candidates:
        exact.setdefault(heading_key(text(n)), []).append(n)
        num = section_number(text(n))
        if num:
            numbered.setdefault(num, []).append(n)
    matched, unresolved = {}, []
    for title, original_nodes in source_rows:
        match = re.match(r'^(.*?)\s*(\d+)\s*$', title)
        if not match:
            continue  # Keep unrecognized source content rather than discard it.
        label = match[1].rstrip(' .\t\u2026')
        choices = exact.get(heading_key(label), [])
        if len(choices) != 1:
            choices = numbered.get(section_number(label), [])
        if len(choices) == 1:
            matched.setdefault(id(choices[0]), label)
            removable.update(id(n) for n in original_nodes)
        else:
            unresolved.append(label)
            # A stale TOC entry must not create a fictitious section or page.
            removable.update(id(n) for n in original_nodes)
    # Missing source entries are generated from headings; a mismatch never truncates the TOC.
    if not targets and toc_title is None:
        toc = None
    else:
        if toc_title is None and containers:
            toc_container = next((n for n in containers if 'cover-content' not in n.attrs.get('class', '')), containers[0])
            toc_title = make('h3', {'id': 'element-0', 'data-element-type': 'h',
                                   'class': classes('fz', 18) + ' ' + classes('text-align', 'center') + ' adaptive-color w-100 lecture-heading',
                                   'style': base_style()}, ['СОДЕРЖАНИЕ'])
            toc_container.children.insert(0, toc_title)
        toc = make('div', {'id': 'element-0', 'data-element-type': 'toc', 'class': 'toc'}) if toc_title is not None else None
    links = []
    for target in ([toc_title] if toc_title is not None and id(toc_title) in matched else []) + targets:
        if toc is None:
            break
        label = matched.get(id(target), text(target))
        level = 1 if target.tag == 'h5' else 0
        page_node = make('span', {'class': 'toc-page'})
        row = make('a', {'class': f'toc-row level-{level}'},
                   [make('span', {'class': 'toc-title'}, [html.escape(label)]), make('span', {'class': 'toc-dots'}), page_node])
        toc.children.append(make('div', {'class': 'toc-item'}, [row]))
        links.append((row, page_node, target))
    for container in containers:
        result = []
        for node in container.children:
            if id(node) not in removable:
                result.append(node)
            if node is toc_title and toc is not None and toc.children:
                result.append(toc)
        container.children = result

    # Remove pages that became empty after normalization or after merging a split TOC.
    removed = 0
    def prune(parent):
        nonlocal removed
        kept = []
        for node in parent.children:
            if isinstance(node, Node):
                if node.attrs.get('data-element-type') == 'page':
                    contents = [n for n in node.walk() if 'lecture-content' in n.attrs.get('class', '').split()]
                    if contents and not any(content_present(n) for n in contents):
                        removed += 1
                        continue
                prune(node)
            kept.append(node)
        parent.children = kept
    prune(tree)
    # IDs, page fields and TOC destinations are finalized only after page removal.
    for i, node in enumerate((n for n in tree.walk() if 'id' in n.attrs), 1):
        node.attrs['id'] = f'element-{i}'
    pages = [n for n in tree.walk() if n.attrs.get('data-element-type') == 'page']
    page_by_node = {}
    for number, page in enumerate(pages, 1):
        for n in page.walk():
            page_by_node[id(n)] = number
            if n.attrs.get('data-field') == 'number':
                n.children = [str(number)]
    for row, page_node, target in links:
        row.attrs['href'] = '#' + target.attrs['id']
        page_node.children = [str(page_by_node[id(target)])]
    if stats is not None:
        stats.update(pages=len(pages), removed_empty_pages=removed, toc_entries=len(links), unresolved_toc=unresolved)
    # Preserve inline text exactly; indent block structure for readable source.
    return pretty(tree).strip()


INLINE = {'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'span', 'a', 'math', 'i'}


def pretty(node, depth=0):
    if isinstance(node, str):
        return ('  ' * depth + node.strip() + '\n') if node.strip() else ''
    if not node.tag:
        return ''.join(pretty(c, depth) for c in node.children)
    if node.tag in INLINE or node.tag in Parser.VOID or not node.children or 'image-label' in node.attrs.get('class', '').split():
        return '  ' * depth + node.render() + '\n'
    shell = copy.copy(node)
    shell.children = []
    opening = shell.render().removesuffix(f'</{node.tag}>')
    return '  ' * depth + opening + '\n' + ''.join(pretty(c, depth + 1) for c in node.children) + '  ' * depth + f'</{node.tag}>\n'
