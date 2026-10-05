import contextlib
import io
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from convert import Converter, NS, Parser, ROOT, Templates, main


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def docx(self, body, name='sample.docx'):
        path = self.root / name
        with ZipFile(path, 'w') as z:
            z.writestr('word/document.xml', '<w:document xmlns:w="' + NS['w'] + '"><w:body>' + body + '</w:body></w:document>')
        return path

    def test_escaping_and_merged_cells(self):
        source = self.docx('''<w:p><w:r><w:t>&lt;script&gt; &amp; текст</w:t></w:r></w:p>
        <w:tbl><w:tblGrid><w:gridCol w:w="100"/><w:gridCol w:w="200"/></w:tblGrid>
        <w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/><w:vMerge w:val="restart"/></w:tcPr>
        <w:p><w:r><w:t>merged</w:t></w:r></w:p></w:tc></w:tr>
        <w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/><w:vMerge/></w:tcPr><w:p/></w:tc></w:tr></w:tbl>''')
        fragment, _ = Converter(source).convert()
        self.assertIn('&lt;script&gt; &amp; текст', fragment)
        self.assertNotIn('<script>', fragment)
        nodes = list(Parser(fragment).root.walk())
        cells = [n for n in nodes if n.attrs.get('data-element-type') == 'tableCol']
        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0].attrs['aria-colspan'], '2')
        self.assertEqual(cells[0].attrs['aria-rowspan'], '2')

    def test_frame_and_page_number(self):
        source = self.docx('<w:p><w:r><w:t>First</w:t><w:br w:type="page"/></w:r></w:p><w:p><w:r><w:t>Second</w:t></w:r></w:p>')
        fragment, info = Converter(source, Templates(ROOT / 'page-template.html')).convert()
        self.assertEqual(info['pages'], 2)
        self.assertIn('data-element-template="form5"', fragment)
        self.assertIn('data-element-template="form5a"', fragment)
        self.assertNotIn('{{', fragment)
        ids = [n.attrs['id'] for n in Parser(fragment).root.walk() if 'id' in n.attrs]
        self.assertEqual(len(ids), len(set(ids)))

    def test_batch_failure_skip_and_fragment(self):
        source = self.docx('<w:p><w:r><w:t>Original</w:t></w:r></w:p>')
        output = self.root / 'html'
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main([str(source), '-o', str(output), '--fragment']), 0)
            dest = output / 'sample.html'
            self.assertTrue(dest.read_text(encoding='utf-8').startswith('<div'))
            dest.write_text('keep', encoding='utf-8')
            self.assertEqual(main([str(source), '-o', str(output)]), 0)
            self.assertEqual(dest.read_text(encoding='utf-8'), 'keep')
            (self.root / 'bad.docx').write_bytes(b'not a zip')
            self.assertEqual(main([str(self.root), '-o', str(output), '--overwrite']), 1)
            self.assertIn('Original', dest.read_text(encoding='utf-8'))

    def test_font_family_is_valid_inline_css(self):
        source = self.docx('<w:p><w:r><w:rPr><w:rFonts w:ascii="Times New Roman"/></w:rPr><w:t>Text</w:t></w:r></w:p>')
        fragment, _ = Converter(source).convert()
        self.assertNotIn('font-family:&#', fragment)
        self.assertIn('fz-lg-16', fragment)

    def test_import_has_no_css_and_preserves_alignment(self):
        source = self.docx(''.join(f'<w:p><w:pPr><w:jc w:val="{align}"/></w:pPr><w:r><w:t>{align}</w:t></w:r></w:p>' for align in ('left', 'center', 'right')))
        output = self.root / 'import'
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main([str(source), '-o', str(output)]), 0)
        fragment = (output / 'sample.html').read_text(encoding='utf-8')
        self.assertNotIn('<style', fragment)
        self.assertNotIn('<head', fragment)
        self.assertTrue(fragment.startswith('<div'))
        for n in Parser(fragment).root.walk():
            if n.attrs.get('data-element-type') == 'text':
                from layout import text, style_values
                align = text(n)
                for device in ('lg', 'tb', 'mb'):
                    self.assertIn(f'text-align-{device}-{align}', n.attrs['class'])
                self.assertNotIn('text-align', style_values(n.attrs.get('style', '')))

    def test_preview_alignment_utilities(self):
        from convert import preview_css
        css = preview_css()
        for device in ('lg', 'tb', 'mb'):
            for align in ('left', 'center', 'right'):
                self.assertIn(f'.text-align-{device}-{align} {{ text-align: {align}; }}', css)
            for align in ('flex-start', 'center', 'flex-end'):
                for prop in ('justify-content', 'align-items'):
                    self.assertIn(f'.{prop}-{device}-{align} {{ {prop}: {align}; }}', css)
        for selector in ('.image-html', '.lecture-content'):
            import re
            for body in re.findall(re.escape(selector) + r'\s*\{([^}]+)\}', css):
                self.assertNotIn('justify-content:', body)
                self.assertNotIn('align-items:', body)

    def test_page_break_inside_run_preserves_text(self):
        source = self.docx('<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Before</w:t><w:lastRenderedPageBreak/><w:t>After</w:t></w:r></w:p>')
        fragment, info = Converter(source).convert()
        pages = [n for n in Parser(fragment).root.walk() if n.attrs.get('data-element-type') == 'page']
        self.assertEqual(info['pages'], 2)
        self.assertIn('Before', pages[0].render())
        self.assertNotIn('After', pages[0].render())
        self.assertIn('After', pages[1].render())
        self.assertNotIn('text-align:', pages[1].render())
        self.assertIn('fw-lg-font-bold', pages[1].render())

    def test_image_is_block_with_caption_and_adaptive_width(self):
        from layout import format_document
        fragment = format_document('''<div id="element-0" data-element-type="page"><div id="element-0" class="lecture-content">
          <p id="element-0" data-element-type="text"><span><span id="element-0" data-element-type="img" style="width:330px"><img src="data:image/png;base64,eA==" alt="image"/></span></span></p>
          <p id="element-0" data-element-type="text"><span>Рисунок 1 - Подпись</span></p></div></div>''')
        tree = Parser(fragment).root
        content = next(n for n in tree.walk() if n.attrs.get('data-element-type') == 'page')
        image = next(n for n in content.children if hasattr(n, 'attrs') and n.attrs.get('data-element-type') == 'img')
        self.assertEqual(image.tag, 'div')
        self.assertIn('--width-lg: 50.0%', image.render())
        self.assertIn('--width-mb: 50.0%', image.render())
        labels = [n for n in tree.walk() if 'image-label' in n.attrs.get('class', '')]
        self.assertEqual(len(labels), 1)
        self.assertIn('Рисунок 1 - Подпись', labels[0].render())
        self.assertNotIn('<p', image.render())

    def test_repeated_word_breaks_do_not_create_empty_pages(self):
        source = self.docx('''<w:p><w:r><w:t>First</w:t><w:br w:type="page"/></w:r></w:p>
          <w:p><w:r><w:t> </w:t></w:r></w:p>
          <w:p><w:pPr><w:pageBreakBefore/></w:pPr><w:bookmarkStart w:id="1"/>
            <w:r><w:lastRenderedPageBreak/><w:t>Second</w:t></w:r></w:p>
          <w:p><w:r><w:br w:type="page"/></w:r></w:p><w:p/>''')
        fragment, info = Converter(source, Templates(ROOT / 'page-template.html')).convert()
        from reference_checks import validate_layout
        self.assertEqual(info['pages'], 2)
        self.assertGreater(info['removed_empty_pages'], 0)
        self.assertEqual(validate_layout(Parser(fragment).root)['pages'], 2)

    def test_toc_crosses_pages_matches_section_numbers_and_updates_pages(self):
        source = self.docx('''<w:p><w:pPr><w:outlineLvl w:val="0"/></w:pPr><w:r><w:t>СОДЕРЖАНИЕ</w:t></w:r></w:p>
          <w:p><w:r><w:t>1 Old title</w:t><w:tab/><w:t>99</w:t><w:br w:type="page"/></w:r></w:p>
          <w:p><w:r><w:t>2 Second section</w:t><w:tab/><w:t>100</w:t></w:r></w:p>
          <w:p><w:r><w:t>9 Deleted section</w:t><w:tab/><w:t>101</w:t><w:br w:type="page"/></w:r></w:p>
          <w:p><w:pPr><w:outlineLvl w:val="0"/></w:pPr><w:r><w:t>1 New title</w:t></w:r></w:p>
          <w:p><w:r><w:t>Body text.</w:t></w:r></w:p>
          <w:p><w:pPr><w:outlineLvl w:val="0"/></w:pPr><w:r><w:t>2 Second section</w:t></w:r></w:p>''')
        fragment, info = Converter(source, Templates(ROOT / 'page-template.html')).convert()
        from reference_checks import validate_layout
        self.assertEqual(info['pages'], 2)
        self.assertEqual(info['toc_entries'], 2)
        self.assertEqual(info['unresolved_toc'], ['9 Deleted section'])
        self.assertNotIn('Deleted section', fragment)
        tree = Parser(fragment).root
        self.assertEqual(validate_layout(tree)['toc_entries'], 2)
        page_labels = [n.render() for n in tree.walk() if n.attrs.get('class') == 'toc-page']
        self.assertEqual(page_labels, ['<span class="toc-page">2</span>'] * 2)

    def test_toc_is_created_from_headings_when_source_has_none(self):
        source = self.docx('''<w:p><w:pPr><w:outlineLvl w:val="0"/></w:pPr><w:r><w:t>1 First</w:t></w:r></w:p>
          <w:p><w:r><w:t>Body</w:t></w:r></w:p>
          <w:p><w:pPr><w:outlineLvl w:val="1"/></w:pPr><w:r><w:t>1.1 Subsection</w:t></w:r></w:p>''')
        fragment, info = Converter(source).convert()
        from reference_checks import validate_layout
        self.assertEqual(info['toc_entries'], 2)
        self.assertEqual(validate_layout(Parser(fragment).root)['toc_entries'], 2)


if __name__ == '__main__':
    unittest.main()
