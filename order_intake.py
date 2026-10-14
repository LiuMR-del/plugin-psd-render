"""铝板画订单规范化与尺寸匹配辅助。

该模块只处理确定性规则：识别订单字段、解析 cm/inch 尺寸、保留横竖方向，
并把 ExcelData 行转换成可用于模板匹配的轻量订单记录。AI 不参与尺寸换算。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable


_SIZE_PAIR_RE = re.compile(
    r"(?P<left>\d+(?:[.,]\d+)?)\s*(?:×|x|X|\*|\s+-\s*|[-—–－])\s*"
    r"(?P<right>\d+(?:[.,]\d+)?)"
)
_UNIT_RE = re.compile(r"(?P<unit>cm|厘米|inch(?:es)?|inches|inch|in|英寸|\")", re.I)
_SIZE_COLUMN_ALIASES = {
    "尺寸", "规格", "size", "dimension", "dimensions", "product size",
    "select size", "选择尺寸",
}


@dataclass(frozen=True)
class SizeSpec:
    """一个带方向的尺寸，内部以毫米整数保存。"""

    width_mm: int
    height_mm: int
    source: str = ""
    unit: str = ""

    @property
    def key(self) -> str:
        return dimension_key_from_mm(self.width_mm, self.height_mm)

    @property
    def cm(self) -> tuple[float, float]:
        return self.width_mm / 10, self.height_mm / 10


@dataclass
class NormalizedOrder:
    row: object
    row_no: int
    sku: str
    size: SizeSpec | None
    custom_fields: dict[str, list[str]] = field(default_factory=dict)
    status: str = "invalid"
    status_reason: str = ""

    @property
    def size_key(self) -> str:
        return self.size.key if self.size else ""


def _decimal(value: str) -> Decimal:
    return Decimal(str(value).replace(",", ".").strip())


def _format_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _cm_to_mm(value: Decimal) -> int:
    return int((value * Decimal("10")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _inch_to_mm(value: Decimal) -> int:
    return int((value * Decimal("25.4")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def dimension_key_from_mm(width_mm: int, height_mm: int) -> str:
    """Return a stable cm-looking key while preserving orientation.

    Common inch-derived dimensions such as 30.48 cm are rounded to the nearest
    whole cm so that ``12x8 inch`` matches a template named ``30-20``. For
    non-standard values the exact millimetre-derived decimal is retained.
    """

    def component(mm: int) -> str:
        cm = Decimal(int(mm)) / Decimal("10")
        nearest = cm.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        if abs(cm - nearest) <= Decimal("0.6"):
            return str(int(nearest))
        return _format_decimal(cm)

    return f"{component(width_mm)}x{component(height_mm)}"


def dimension_key_from_cm(width: str | int | float | Decimal,
                           height: str | int | float | Decimal) -> str:
    return dimension_key_from_mm(_cm_to_mm(_decimal(str(width))), _cm_to_mm(_decimal(str(height))))


def _find_pair(text: str) -> tuple[Decimal, Decimal] | None:
    match = _SIZE_PAIR_RE.search(str(text or ""))
    if not match:
        return None
    try:
        return _decimal(match.group("left")), _decimal(match.group("right"))
    except InvalidOperation:
        return None


def _unit_before_or_after(text: str, pair_end: int) -> str:
    tail = str(text or "")[pair_end:pair_end + 16]
    match = _UNIT_RE.search(tail)
    if match:
        return match.group("unit").casefold()
    return ""


def _extract_explicit_unit_pair(text: str, unit: str) -> tuple[Decimal, Decimal] | None:
    raw = str(text or "")
    for match in _SIZE_PAIR_RE.finditer(raw):
        detected = _unit_before_or_after(raw, match.end())
        if detected != unit:
            continue
        try:
            return _decimal(match.group("left")), _decimal(match.group("right"))
        except InvalidOperation:
            return None
    return None


def parse_size(text: str) -> SizeSpec | None:
    """Parse a size string, preferring an explicit cm pair over inch pair."""

    raw = str(text or "").strip()
    if not raw:
        return None

    # Prefer the explicit cm value, including forms such as (30x20cm).
    cm_pair = _extract_explicit_unit_pair(raw, "cm")
    if cm_pair:
        return SizeSpec(_cm_to_mm(cm_pair[0]), _cm_to_mm(cm_pair[1]), raw, "cm")

    # Chinese unit names and a quoted inch suffix are normalized here.
    for unit in ("厘米", "英寸", "inch", "inches", "inch", "in", '"'):
        pair = _extract_explicit_unit_pair(raw, unit.casefold())
        if pair:
            if unit in {"厘米"}:
                return SizeSpec(_cm_to_mm(pair[0]), _cm_to_mm(pair[1]), raw, "cm")
            return SizeSpec(_inch_to_mm(pair[0]), _inch_to_mm(pair[1]), raw, "inch")

    # If the unit is attached before the pair (e.g. "inch 12x8"), infer it.
    pair = _find_pair(raw)
    if not pair:
        return None
    lower = raw.casefold()
    if any(token in lower for token in ("inch", " in", "英寸", '"')):
        return SizeSpec(_inch_to_mm(pair[0]), _inch_to_mm(pair[1]), raw, "inch")
    if "cm" in lower or "厘米" in raw:
        return SizeSpec(_cm_to_mm(pair[0]), _cm_to_mm(pair[1]), raw, "cm")
    return None


def normalize_header(value: str) -> str:
    return " ".join(str(value or "").replace("\u00a0", " ").split()).casefold()


def find_size_column(headers: Iterable[str]) -> str:
    normalized = {normalize_header(header): str(header) for header in headers if str(header or "").strip()}
    for alias in _SIZE_COLUMN_ALIASES:
        found = normalized.get(normalize_header(alias))
        if found:
            return found
    return ""


def custom_field_values(text: str) -> dict[str, list[str]]:
    """Parse simple key/value lines for AI context without deciding layer mappings."""
    fields: dict[str, list[str]] = {}
    current_key = ""
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        positions = [pos for pos in (line.find(":"), line.find("：")) if pos >= 0]
        if positions:
            split_at = min(positions)
            key = line[:split_at].strip()
            value = line[split_at + 1:].strip()
            if key:
                current_key = key
                if value:
                    fields.setdefault(key, []).append(value)
                continue
        if current_key and line:
            fields.setdefault(current_key, []).append(line)
    return fields


def is_aluminum_order(headers: Iterable[str], rows: Iterable[object] = ()) -> bool:
    """Conservative signal used by the UI; it never rejects a valid SKU order."""
    names = {normalize_header(header) for header in headers}
    has_sku = "sku" in names
    has_size = bool(find_size_column(headers))
    has_custom = bool(names & {normalize_header("定制文案"), normalize_header("定制信息")})
    return has_sku and has_size and has_custom


def normalize_orders(excel_data) -> tuple[list[NormalizedOrder], dict]:
    """Convert an existing ExcelData object into order records and summary."""
    size_column = find_size_column(excel_data.headers)
    result: list[NormalizedOrder] = []
    for row in excel_data.rows:
        raw_size = row.values.get(size_column, "") if size_column else ""
        size = parse_size(raw_size)
        if not size:
            # Some Excel readers keep reserved columns out of values; support
            # a direct attribute fallback without changing ExcelData semantics.
            raw_size = getattr(row, "size_raw", "") or raw_size
            size = parse_size(raw_size)
        fields = custom_field_values(row.custom_info)
        if not row.sku:
            status, reason = "invalid", "缺少 SKU"
        elif not size:
            status, reason = "invalid", f"尺寸无法解析: {raw_size or '空'}"
        else:
            status, reason = "unmatched", "尚未匹配模板"
        result.append(NormalizedOrder(row, row.row_no, row.sku, size, fields, status, reason))

    by_sku: dict[str, dict] = {}
    for order in result:
        bucket = by_sku.setdefault(order.sku, {
            "sku": order.sku,
            "rows": [],
            "size_keys": [],
            "sizes": {},
        })
        bucket["rows"].append(order.row_no)
        if order.size_key and order.size_key not in bucket["size_keys"]:
            bucket["size_keys"].append(order.size_key)
        if order.size_key:
            bucket["sizes"].setdefault(order.size_key, []).append(order.row_no)

    return result, {
        "is_aluminum_order": is_aluminum_order(excel_data.headers, excel_data.rows),
        "size_column": size_column,
        "order_count": len(result),
        "sku_count": len(by_sku),
        "by_sku": by_sku,
    }
