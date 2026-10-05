import json
from pathlib import Path
import tempfile
import unittest

from convert import Parser
from split_html import pages_in, signature, split_document, write_parts


def document(count):
    return '\n'.join(f'<div id="page-{i}" data-element-type="page"><span data-field="number">{i}</span>'
                     f'<p id="heading-{i}" data-element-type="h">Page {i}</p>'
                     '<img src="data:image/png;base64,eA==" alt="picture">'
                     '</div>' for i in range(1, count + 1))


class PdfPartsTests(unittest.TestCase):
    def test_31_pages_are_split_into_seven_parts_without_changes(self):
        source = document(31)
        parts = split_document(source, 5)
        self.assertEqual([info['pages'] for _, info in parts], [5, 5, 5, 5, 5, 5, 1])
        self.assertEqual(parts[-1][1]['first_page'], 31)
        original = [signature(p) for p in pages_in(source)]
        joined = [signature(p) for fragment, _ in parts for p in pages_in(fragment)]
        self.assertEqual(joined, original)

    def test_external_part_toc_links_are_disabled_but_labels_remain(self):
        source = document(2).replace('<p id="heading-1"', '<div data-element-type="toc"><a class="toc-row" href="#heading-2"><span class="toc-title">Second</span><span class="toc-page">2</span></a><a href="#heading-1">Local</a></div><p id="heading-1"', 1)
        parts = split_document(source, 1)
        fragment = parts[0][0]
        self.assertNotIn('href="#heading-2"', fragment)
        self.assertIn('href="#heading-1"', fragment)
        self.assertIn('<span class="toc-title">Second</span>', fragment)
        self.assertIn('<span class="toc-page">2</span>', fragment)
        self.assertEqual(parts[0][1]['disabled_cross_part_links'], 1)

    def test_saved_file_links_become_local(self):
        source = document(1).replace('</div>', '<a href="file:///old/document.html#heading-1">Jump</a></div>')
        fragment, _ = split_document(source)[0]
        self.assertIn('href="#heading-1"', fragment)
        self.assertNotIn('file:', fragment)

    def test_manifest_matches_written_files_and_rerun_removes_only_old_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            first = write_parts(document(11), path, 5)
            self.assertEqual(len(first['parts']), 3)
            (path / 'my-note.txt').write_text('keep', encoding='utf-8')
            second = write_parts(document(6), path, 5)
            self.assertEqual(len(list(path.glob('*.html'))), 2)
            self.assertEqual((path / 'my-note.txt').read_text(encoding='utf-8'), 'keep')
            self.assertEqual(json.loads((path / 'manifest.json').read_text(encoding='utf-8')), second)
            for info in second['parts']:
                pages = pages_in((path / info['file']).read_text(encoding='utf-8'))
                self.assertEqual([signature(p) for p in pages], info['page_signatures'])

    def test_invalid_input_is_rejected(self):
        for size in (0, 11):
            with self.assertRaises(ValueError):
                split_document(document(1), size)
        with self.assertRaises(ValueError):
            split_document('<p>No pages</p>')
        with self.assertRaises(ValueError):
            split_document('<div data-element-type="page">' + document(1) + '</div>')

    def test_converter_creates_parts_for_existing_output(self):
        import contextlib
        import io
        from zipfile import ZipFile
        from convert import NS, main
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / 'input.docx'
            with ZipFile(source, 'w') as z:
                z.writestr('word/document.xml', '<w:document xmlns:w="' + NS['w'] + '"><w:body><w:p><w:r><w:t>Hello</w:t></w:r></w:p></w:body></w:document>')
            output = folder / 'output'
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main([str(source), '-o', str(output)]), 0)
                self.assertEqual(main([str(source), '-o', str(output), '--pdf-parts', str(folder / 'parts')]), 0)
            manifest = json.loads((folder / 'parts/input/manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['pages'], 1)
            self.assertEqual(len(manifest['parts']), 1)


if __name__ == '__main__':
    unittest.main()
