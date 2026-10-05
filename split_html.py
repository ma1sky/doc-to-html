"""Split editor HTML into small documents without rasterizing or dropping pages."""
import argparse
from collections import Counter
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from urllib.parse import unquote

from convert import Node, Parser


def pages_in(source):
    tree = Parser(source).root
    return [n for n in tree.walk() if n.attrs.get('data-element-type') == 'page']


def signature(page):
    """Check editable elements, text, pictures, styles and original page fields."""
    def text_content(n):
        return n if isinstance(n, str) else ''.join(text_content(c) for c in n.children)
    elements = [(n.tag, n.attrs) for n in page.walk() if 'data-element-type' in n.attrs or n.tag in ('img', 'math') or 'data-field' in n.attrs]
    return hashlib.sha256(json.dumps([text_content(page), elements], ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def split_document(source, pages_per_part=5):
    if not 1 <= pages_per_part <= 10:
        raise ValueError('Размер части должен быть от 1 до 10 страниц; рекомендуемое значение — 5.')
    pages = pages_in(source)
    if not pages:
        raise ValueError('Не найдены блоки data-element-type="page".')
    # Nested page blocks would otherwise duplicate their contents in the result.
    if any(any(n is not p and n.attrs.get('data-element-type') == 'page' for n in p.walk()) for p in pages):
        raise ValueError('Обнаружены вложенные страницы: проверьте структуру исходного HTML.')
    all_ids = {n.attrs['id'] for p in pages for n in p.walk() if 'id' in n.attrs}
    result = []
    for start in range(0, len(pages), pages_per_part):
        selected = copy.deepcopy(pages[start:start + pages_per_part])
        local_ids = {n.attrs['id'] for p in selected for n in p.walk() if 'id' in n.attrs}
        disabled_links = 0
        for page in selected:
            for n in page.walk():
                href = n.attrs.get('href', '')
                if n.tag == 'a' and '#' in href:
                    target = unquote(href.rsplit('#', 1)[1])
                    internal = href.startswith('#') or (href.startswith('file:') and target in all_ids)
                    if internal and target in local_ids:
                        n.attrs['href'] = '#' + target
                    elif internal:
                        # Native editor TOCs also support div.toc-row. Keep the
                        # title/page number, but do not leave a broken local link.
                        n.tag = 'div' if 'toc-row' in n.attrs.get('class', '').split() else 'span'
                        n.attrs.pop('href', None)
                        disabled_links += 1
        fragment = '\n'.join(p.render() for p in selected) + '\n'
        reparsed = pages_in(fragment)
        assert [signature(p) for p in reparsed] == [signature(p) for p in pages[start:start + pages_per_part]], 'Содержимое страницы изменилось при разбиении'
        types = Counter(n.attrs['data-element-type'] for p in selected for n in p.walk() if 'data-element-type' in n.attrs)
        result.append((fragment, {
            'first_page': start + 1, 'last_page': start + len(selected), 'pages': len(selected),
            'disabled_cross_part_links': disabled_links, 'element_counts': dict(types),
            'page_signatures': [signature(p) for p in reparsed],
            # Estimate only: actual height also depends on the service's CSS.
            'estimated_canvas_at_width_800_scale_2': {'width': 1600, 'height': math.ceil(800 * math.sqrt(2) * 2 * len(selected))},
        }))
    return result


def write_parts(source, destination, pages_per_part=5, source_name='document.html'):
    parts = split_document(source, pages_per_part)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    old_manifest = destination / 'manifest.json'
    previous = json.loads(old_manifest.read_text(encoding='utf-8')) if old_manifest.exists() else {}
    manifest = {'source': source_name, 'source_sha256': hashlib.sha256(source.encode('utf-8')).hexdigest(),
                'pages': sum(info['pages'] for _, info in parts), 'pages_per_part': pages_per_part,
                'export_tested_in_service': False, 'parts': []}
    for index, (fragment, info) in enumerate(parts, 1):
        filename = f'part-{index:03d}-pages-{info["first_page"]:03d}-{info["last_page"]:03d}.html'
        path = destination / filename
        path.write_text(fragment, encoding='utf-8')
        assert [signature(p) for p in pages_in(path.read_text(encoding='utf-8'))] == info['page_signatures']
        manifest['parts'].append({'file': filename, **info})
    old_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    active = {part['file'] for part in manifest['parts']}
    # Remove only obsolete generated parts listed by our previous manifest.
    for part in previous.get('parts', []):
        filename = part.get('file', '')
        if filename not in active and re.fullmatch(r'part-\d+-pages-\d+-\d+\.html', filename):
            stale = destination / filename
            if stale.resolve().parent == destination.resolve() and stale.is_file():
                stale.unlink()
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description='Разбиение HTML редактора на небольшие части для экспорта PDF.')
    parser.add_argument('input', type=Path, help='HTML или папка с HTML (только файлы непосредственно в папке)')
    parser.add_argument('-o', '--output', type=Path, default=Path(__file__).resolve().parent / 'pdf-parts')
    parser.add_argument('--pages', type=int, default=5, help='Страниц в части: 1–10, по умолчанию 5')
    args = parser.parse_args(argv)
    if not 1 <= args.pages <= 10:
        parser.error('--pages должен быть от 1 до 10')
    if not args.input.exists():
        parser.error('Исходный путь не существует')
    files = [args.input] if args.input.is_file() else sorted(args.input.glob('*.html'))
    if not files:
        parser.error('HTML-файлы не найдены')
    summary = []
    for path in files:
        dest = args.output / path.stem
        try:
            manifest = write_parts(path.read_text(encoding='utf-8-sig'), dest, args.pages, str(path))
            entry = {'source': str(path), 'directory': str(dest), 'status': 'ok', 'pages': manifest['pages'], 'parts': len(manifest['parts'])}
            print(f'{path.name}: {entry["pages"]} стр., частей: {entry["parts"]}', flush=True)
        except Exception as exc:
            entry = {'source': str(path), 'status': 'error', 'error': str(exc)}
            print(f'Ошибка: {path.name}: {exc}', flush=True)
        summary.append(entry)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'report.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return int(any(e['status'] == 'error' for e in summary))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    raise SystemExit(main())
