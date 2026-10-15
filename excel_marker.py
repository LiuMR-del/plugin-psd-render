"""Fill whole order rows yellow in the original workbook after a run (2026-10-15).

openpyxl drops the product pictures embedded in the order workbooks, so the
file is edited at the XML level instead: one solid fill is added to
``xl/styles.xml``, each style used by a marked row gets a copy with that fill,
and the cells of the marked rows (A..last column of the sheet) point to the
copies.  Every other zip entry is copied byte for byte.  The original is
backed up first and replaced atomically.  Only the first worksheet is touched
(the one ``excel_data.parse_excel`` reads).
"""
from __future__ import annotations

import os
import posixpath
import re
import shutil
import tempfile
import zipfile
from datetime import datetime
from typing import Iterable

YELLOW = "FFFFFF00"
BACKUP_DIRNAME = "备份"

_FILL_TEMPLATE = ('<fill><patternFill patternType="solid"><fgColor rgb="{rgb}"/>'
                  '<bgColor indexed="64"/></patternFill></fill>')
_XF_RE = re.compile(r"<xf\b[^>]*?/>|<xf\b[^>]*?>.*?</xf>", re.S)
_FILL_RE = re.compile(r"<fill\b[^>]*?/>|<fill\b[^>]*?>.*?</fill>", re.S)
_CELL_RE = re.compile(r"<c\b[^>]*?/>|<c\b[^>]*?>.*?</c>", re.S)
_ROW_RE = re.compile(r'<row\b(?P<attrs>[^>]*?)(?:/>|>(?P<body>.*?)</row>)', re.S)


class MarkError(Exception):
    pass


def _col_index(letters: str) -> int:
    value = 0
    for ch in letters.upper():
        value = value * 26 + ord(ch) - 64
    return value


def _col_letters(index: int) -> str:
    out = ""
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def _attr(tag: str, name: str) -> str | None:
    match = re.search(r'\s%s="([^"]*)"' % re.escape(name), tag)
    return match.group(1) if match else None


def _set_attr(tag: str, name: str, value: str) -> str:
    """Set ``name="value"`` on the opening tag at the start of ``tag``."""
    head_end = tag.index(">")
    head = tag[:head_end]
    if _attr(head, name) is not None:
        head = re.sub(r'(\s%s=")[^"]*(")' % re.escape(name), lambda m: m.group(1) + value + m.group(2), head)
    else:
        closing = "/" if head.endswith("/") else ""
        head = (head[:-1] if closing else head) + f' {name}="{value}"' + closing
    return head + tag[head_end:]


def _first_sheet_path(archive: zipfile.ZipFile) -> str:
    workbook = archive.read("xl/workbook.xml").decode("utf-8")
    sheet = re.search(r"<sheet\b[^>]*>", workbook)
    rel_id = _attr(sheet.group(0), "r:id") if sheet else None
    rels = archive.read("xl/_rels/workbook.xml.rels").decode("utf-8")
    for rel in re.findall(r"<Relationship\b[^>]*>", rels):
        if _attr(rel, "Id") == rel_id:
            target = _attr(rel, "Target") or ""
            return target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
    raise MarkError("找不到订单表的第一个工作表")


def _add_fill_styles(styles: str, rgb: str) -> tuple[str, dict[int, int], set[int]]:
    """Return (new styles.xml, {old xf index: marked xf index}, marked xf indexes)."""
    fills_match = re.search(r"<fills\b[^>]*>(.*?)</fills>", styles, re.S)
    xfs_match = re.search(r"<cellXfs\b[^>]*>(.*?)</cellXfs>", styles, re.S)
    if not fills_match or not xfs_match:
        raise MarkError("订单表样式结构无法识别")
    fill_xml = _FILL_TEMPLATE.format(rgb=rgb)
    fills = _FILL_RE.findall(fills_match.group(1))
    if fill_xml in fills:
        fill_id = fills.index(fill_xml)
    else:
        fill_id = len(fills)
        fills.append(fill_xml)
    xfs = _XF_RE.findall(xfs_match.group(1))
    marked = {i for i, xf in enumerate(xfs) if _attr(xf.split(">", 1)[0], "fillId") == str(fill_id)}
    mapping: dict[int, int] = {}
    for index, xf in enumerate(list(xfs)):
        if index in marked:
            mapping[index] = index
            continue
        copy = _set_attr(_set_attr(xf, "fillId", str(fill_id)), "applyFill", "1")
        if copy in xfs:
            mapping[index] = xfs.index(copy)
        else:
            mapping[index] = len(xfs)
            xfs.append(copy)
        marked.add(mapping[index])
    new_fills = f'<fills count="{len(fills)}">' + "".join(fills) + "</fills>"
    new_xfs = f'<cellXfs count="{len(xfs)}">' + "".join(xfs) + "</cellXfs>"
    styles = styles[:fills_match.start()] + new_fills + styles[fills_match.end():]
    xfs_match = re.search(r"<cellXfs\b[^>]*>(.*?)</cellXfs>", styles, re.S)
    return styles[:xfs_match.start()] + new_xfs + styles[xfs_match.end():], mapping, marked


def _last_column(sheet: str) -> int:
    dimension = re.search(r'<dimension\b[^>]*\sref="([^"]+)"', sheet)
    last = 1
    if dimension:
        letters = re.findall(r"([A-Z]+)\d+", dimension.group(1))
        if letters:
            last = max(_col_index(item) for item in letters)
    for ref in re.findall(r'<c\b[^>]*?\sr="([A-Z]+)\d+"', sheet):
        last = max(last, _col_index(ref))
    return last


def _mark_sheet(sheet: str, rows: set[int], mapping: dict[int, int]) -> tuple[str, set[int]]:
    last = _last_column(sheet)
    done: set[int] = set()

    def mark_row(match: re.Match) -> str:
        attrs = match.group("attrs")
        row_no = int(_attr(attrs, "r") or 0)
        if row_no not in rows:
            return match.group(0)
        done.add(row_no)
        cells: dict[int, str] = {}
        for cell in _CELL_RE.findall(match.group("body") or ""):
            ref = _attr(cell.split(">", 1)[0], "r") or ""
            col = _col_index(re.match(r"[A-Z]+", ref).group(0)) if ref else max(cells, default=0) + 1
            style = int(_attr(cell.split(">", 1)[0], "s") or 0)
            cells[col] = _set_attr(cell, "s", str(mapping.get(style, style)))
        for col in range(1, last + 1):
            cells.setdefault(col, f'<c r="{_col_letters(col)}{row_no}" s="{mapping.get(0, 0)}"/>')
        return f"<row{attrs}>" + "".join(cells[col] for col in sorted(cells)) + "</row>"

    return _ROW_RE.sub(mark_row, sheet), done


def mark_rows(path: str, row_numbers: Iterable[int], rgb: str = YELLOW,
              backup_dir: str | None = None) -> dict:
    """Fill ``row_numbers`` (1-based sheet rows) with ``rgb``; back up the file first.

    Returns ``{"marked": [...], "missing": [...], "backup": path}``.
    """
    rows = {int(item) for item in row_numbers if int(item) > 0}
    if not rows:
        return {"marked": [], "missing": [], "backup": ""}
    with zipfile.ZipFile(path) as archive:
        sheet_path = _first_sheet_path(archive)
        styles, mapping, _ = _add_fill_styles(archive.read("xl/styles.xml").decode("utf-8"), rgb)
        sheet, done = _mark_sheet(archive.read(sheet_path).decode("utf-8"), rows, mapping)
        backup_dir = backup_dir or os.path.join(os.path.dirname(os.path.abspath(path)), BACKUP_DIRNAME)
        os.makedirs(backup_dir, exist_ok=True)
        stem, ext = os.path.splitext(os.path.basename(path))
        base = os.path.join(backup_dir, f"{stem}-{datetime.now():%Y%m%d-%H%M%S}")
        backup, counter = base + ext, 1
        while os.path.exists(backup):
            counter += 1
            backup = f"{base}-{counter}{ext}"
        shutil.copy2(path, backup)
        handle, temp = tempfile.mkstemp(suffix=ext, dir=os.path.dirname(os.path.abspath(path)))
        os.close(handle)
        try:
            with zipfile.ZipFile(temp, "w") as out:
                for info in archive.infolist():
                    if info.filename == "xl/styles.xml":
                        data = styles.encode("utf-8")
                    elif info.filename == sheet_path:
                        data = sheet.encode("utf-8")
                    else:
                        data = archive.read(info)
                    out.writestr(info, data, compress_type=info.compress_type)
        except BaseException:
            os.unlink(temp)
            raise
    shutil.copymode(path, temp)
    os.replace(temp, path)
    return {"marked": sorted(done), "missing": sorted(rows - done), "backup": backup}


def lock_file(path: str) -> str:
    """Lock file Office/WPS/LibreOffice leave next to an open workbook, or ""."""
    folder, name = os.path.split(os.path.abspath(path))
    for candidate in (f"~${name}", f".~{name}", f".~lock.{name}#"):
        full = os.path.join(folder, candidate)
        if os.path.exists(full):
            return full
    return ""


__all__ = ["BACKUP_DIRNAME", "MarkError", "YELLOW", "lock_file", "mark_rows"]
