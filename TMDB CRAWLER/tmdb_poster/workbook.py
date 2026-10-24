from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile, ZipFile

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
HEADERS = {"제목", "작품명", "작품제목", "애니제목", "애니메이션제목", "title", "name", "한글제목"}


@dataclass(frozen=True)
class ExcelTitle:
    row: int
    title: str


@dataclass(frozen=True)
class WorkbookTitles:
    path: Path
    sheet: str
    items: list[ExcelTitle]


def read_titles(path: Path, sheet: str | None = None) -> WorkbookTitles:
    """첫 시트 A열의 문자열만 읽습니다. 수식/매크로를 실행하거나 파일을 수정하지 않습니다."""
    path = Path(path).expanduser().resolve()
    if path.suffix.lower() != ".xlsx":
        raise ValueError(".xlsx 형식의 엑셀 파일을 선택해 주세요.")
    try:
        with ZipFile(path) as archive:
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            targets = {node.get("Id"): node.get("Target") for node in relationships.findall(f"{{{PACKAGE_REL}}}Relationship")
                       if node.get("TargetMode") != "External"}
            sheets = workbook.findall(f"{{{MAIN}}}sheets/{{{MAIN}}}sheet")
            selected = next((node for node in sheets if sheet is None or node.get("name") == sheet), None)
            if selected is None:
                raise ValueError("읽을 수 있는 시트를 찾지 못했습니다.")
            target = targets.get(selected.get(f"{{{REL}}}id"))
            if not target:
                raise ValueError("시트의 데이터를 찾지 못했습니다.")
            target = target.replace("\\", "/")
            worksheet = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
            shared = []
            if "xl/sharedStrings.xml" in archive.namelist():
                strings = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                shared = ["".join(text.text or "" for text in node.iter(f"{{{MAIN}}}t"))
                          for node in strings.findall(f"{{{MAIN}}}si")]
            root = ET.fromstring(archive.read(worksheet))
            items = []
            for row in root.findall(f"{{{MAIN}}}sheetData/{{{MAIN}}}row"):
                for cell in row.findall(f"{{{MAIN}}}c"):
                    reference = cell.get("r", "")
                    if not re.fullmatch(r"A[1-9][0-9]*", reference):
                        continue
                    kind = cell.get("t", "")
                    value_node = cell.find(f"{{{MAIN}}}v")
                    value = value_node.text if value_node is not None else ""
                    if kind == "s" and value:
                        value = shared[int(value)]
                    elif kind == "inlineStr":
                        value = "".join(node.text or "" for node in cell.findall(f"{{{MAIN}}}is//{{{MAIN}}}t"))
                    elif kind not in ("str", "n", ""):
                        continue
                    value = (value or "").strip()
                    if not value:
                        continue
                    if not items and re.sub(r"[\s_]", "", value).casefold() in HEADERS:
                        continue
                    items.append(ExcelTitle(int(reference[1:]), value))
            if not items:
                raise ValueError("이 시트의 A열에 읽을 제목이 없습니다.")
            return WorkbookTitles(path, selected.get("name", ""), items)
    except (BadZipFile, ET.ParseError, KeyError, IndexError, TypeError) as error:
        raise ValueError("엑셀 파일을 읽지 못했습니다. 올바른 .xlsx 파일인지 확인해 주세요.") from error
