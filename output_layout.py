"""Output folder layout: {输出目录}/{日期}/{店铺}/{品类}/图片.png (2026-10-15 user request).

* 日期: the day the run starts, written ``10.15`` (month.day, like the order
  workbook names).
* 店铺: the row's ``店铺名`` column; empty -> ``未知店铺``.
* 品类: the order workbook's file name without its leading date
  (``10.15毛毯.xlsx`` -> ``毛毯``); editable in the UI before the run.
"""
from __future__ import annotations

import os
import re
from datetime import date

import excel_data

SHOP_COLUMN = "店铺名"
UNKNOWN_SHOP = "未知店铺"
UNKNOWN_CATEGORY = "未分类"

# Leading date of an order workbook name: 10.15 / 10-15 / 2026.10.15 / 2026-10-15 / 10月15日
_LEADING_DATE_RE = re.compile(
    r"^\s*(?:\d{4}\s*[-._/年]\s*)?\d{1,2}\s*[-._/月]\s*\d{1,2}\s*日?[\s\-_—.、]*"
)


def date_folder(day: date | None = None) -> str:
    day = day or date.today()
    return f"{day.month}.{day.day}"


def folder_name(value: str, fallback: str) -> str:
    """A safe single folder name (no path separators, no trailing dots/spaces)."""
    text = excel_data.sanitize_filename(" ".join(str(value or "").split())).strip(" .")
    return text or fallback


def category_from_excel(path: str) -> str:
    stem = os.path.splitext(os.path.basename(str(path or "")))[0]
    return folder_name(_LEADING_DATE_RE.sub("", stem, count=1), UNKNOWN_CATEGORY)


def shop_of(row) -> str:
    return folder_name(str((getattr(row, "values", None) or {}).get(SHOP_COLUMN) or ""), UNKNOWN_SHOP)


def day_dir(root: str, day: date | None = None) -> str:
    return os.path.join(root, date_folder(day))


def row_dir(root: str, row, category: str, day: date | None = None) -> str:
    return os.path.join(day_dir(root, day), shop_of(row), folder_name(category, UNKNOWN_CATEGORY))


__all__ = [
    "SHOP_COLUMN", "UNKNOWN_CATEGORY", "UNKNOWN_SHOP",
    "category_from_excel", "date_folder", "day_dir", "folder_name", "row_dir", "shop_of",
]
