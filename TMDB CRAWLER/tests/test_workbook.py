from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZipFile

from tmdb_poster.workbook import read_titles


def write_workbook(path, titles):
    cells = '<row r="1"><c r="A1" t="inlineStr"><is><t>작품명</t></is></c></row>'
    for row, title in enumerate(titles, 2):
        cells += f'<row r="{row}"><c r="A{row}" t="inlineStr"><is><t>{escape(title)}</t></is></c></row>'
    write_parts(path, cells)


def write_parts(path, cells, shared=None):
    with ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="목록" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{cells}</sheetData></worksheet>')
        if shared:
            archive.writestr("xl/sharedStrings.xml", shared)


class WorkbookTests(unittest.TestCase):
    def test_column_a_shared_rich_inline_numeric_and_sparse_rows(self):
        cells = '''<row r="1"><c r="A1" t="s"><v>0</v></c></row>
        <row r="2"><c r="A2" t="s"><v>1</v></c><c r="AA2" t="inlineStr"><is><t>읽지 않음</t></is></c></row>
        <row r="4"><c r="A4" t="inlineStr"><is><t>원래_한글_제목</t></is></c></row>
        <row r="7"><c r="A7" t="inlineStr"><is><t> </t></is></c></row>
        <row r="8"><c r="A8"><v>86</v></c></row>'''
        shared = '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>작품명</t></si><si>\n<r><t>약사의 </t></r>\n<r><t>혼잣말</t></r>\n</si></sst>'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "제목 목록.xlsx"
            write_parts(path, cells, shared)
            before = path.read_bytes()
            result = read_titles(path)
            self.assertEqual(result.sheet, "목록")
            self.assertEqual([(item.row, item.title) for item in result.items], [(2, "약사의 혼잣말"), (4, "원래_한글_제목"), (8, "86")])
            self.assertEqual(path.read_bytes(), before)

    def test_headerless_workbook_does_not_drop_first_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "목록.xlsx"
            write_parts(path, '<row r="1"><c r="A1" t="inlineStr"><is><t>약사의 혼잣말</t></is></c></row>')
            self.assertEqual(read_titles(path).items[0].title, "약사의 혼잣말")

    def test_invalid_or_empty_workbook_has_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "목록.xlsx"
            path.write_text("invalid", encoding="utf-8")
            with self.assertRaises(ValueError):
                read_titles(path)
            write_workbook(path, [])
            with self.assertRaisesRegex(ValueError, "A열"):
                read_titles(path)


if __name__ == "__main__":
    unittest.main()
