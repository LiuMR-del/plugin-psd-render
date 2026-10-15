"""Deterministic order text fields -> PSD text layers (no AI, no Photoshop).

Only name/year fields are handled here; they are the fields whose wrong
placement silently produced bad images (a man's name written into the year
layer, a woman's name into a stray layer).  Rules:

* Field kind comes from the order label: ``Custom Year`` / ``EST.Year`` /
  ``Custom Date`` -> year; ``Custom Man's Name`` / ``Custom Name - Male`` ->
  man's name; ``Enter Name`` / ``Title`` -> generic name.
* Layer role comes from the visible text layer itself: a 4-digit year or
  ``since``/``est`` in its name or text -> year layer, otherwise name layer.
* One year field + one year layer -> mapped.  A man's and a woman's name +
  exactly two horizontally separated name layers -> left/right (left = man
  unless the PSD's Man素材 group sits right of Woman素材).  One generic name +
  one name layer -> mapped.  Anything else is unresolved and left to AI /
  manual review; nothing is guessed.

``resolve_order_text`` applies reviewed or local mappings to one order and
checks the result: every name/year field must land on its own visible text
layer of the matching role, otherwise the order is blocked.

Other text fields (``Initial``, ``Sign``, ``Sunrise``...) are written only when
the template profile maps them (AI analysis or review); their kind is
``text`` and any visible text layer is allowed.

Text style fields (2026-10-15): ``Text Color`` / ``Font Color`` / ``Name
Color`` set the text colour, ``Font`` the font family.  The value is used
directly (a ``#RRGGBB`` in it, or the font name); a colour name without a hex
value needs the profile's ``color_values`` (filled by AI analysis or review).
The style applies to the name layers written for the order (all written
layers when there is no name), unless the profile's ``style_mappings`` names
the target layers.  An ambiguous label such as ``Color`` is a style field
only when the profile says so.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping

_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_YEAR_LAYER_WORDS = {"since", "est", "established", "year", "date"}
_YEAR_FIELD_WORDS = {"year", "date", "est", "since", "established"}
_NAME_FIELD_WORDS = {"name", "names", "title"}
_NOT_TEXT_WORDS = {
    "font", "color", "colour", "style", "upload", "photo", "size", "confirm",
    "request", "requests", "option", "options", "look", "product", "type",
}
_MAN_WORDS = {"man", "male", "his", "husband", "groom", "mr", "he", "him"}
_WOMAN_WORDS = {"woman", "female", "her", "wife", "bride", "mrs", "she"}

YEAR = "year"
NAME_MAN = "name_man"
NAME_WOMAN = "name_woman"
NAME = "name"
TEXT = "text"
KIND_LABELS = {YEAR: "年份", NAME_MAN: "男方姓名", NAME_WOMAN: "女方姓名", NAME: "姓名", TEXT: "文字"}
NAME_KINDS = (NAME, NAME_MAN, NAME_WOMAN)

STYLE_COLOR = "color"
STYLE_FONT = "font"
STYLE_LABELS = {STYLE_COLOR: "文字颜色", STYLE_FONT: "字体"}
_COLOR_WORDS = {"color", "colour"}
_STYLE_TARGET_WORDS = {"font", "text", "name", "names", "letter", "letters", "lettering", "title"}
_HEX_RE = re.compile(r"#([0-9a-f]{6}|[0-9a-f]{3})(?![0-9a-z])", re.IGNORECASE)
# Labels that never carry text to write, even when an AI maps them as text.
NOT_TEXT_CONTENT_WORDS = {"font", "color", "colour", "size", "confirm", "photo", "upload", "image", "picture"}


def _tokens(value: Any) -> set[str]:
    text = str(value or "").casefold().replace("’", "'")
    return set(re.findall(r"[a-z0-9]+", text))


def _path(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part for part in value.replace("\\", "/").split("/") if part]
    return [str(part) for part in value or [] if str(part)]


def field_key(section: str, label: str) -> str:
    """Same serialization as ``order_pipeline.serialize_field_key``."""
    return json.dumps([str(section or "").strip(), str(label or "").strip()],
                      ensure_ascii=False, separators=(",", ":"))


def _norm_label(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def classify_field(label: str) -> str:
    """Return the text kind of an order field label, or "" when not name/year."""
    tokens = _tokens(label)
    if not tokens or tokens & _NOT_TEXT_WORDS:
        return ""
    if tokens & _YEAR_FIELD_WORDS:
        return YEAR
    if tokens & _NAME_FIELD_WORDS:
        if tokens & _WOMAN_WORDS:
            return NAME_WOMAN
        if tokens & _MAN_WORDS:
            return NAME_MAN
        return NAME
    return ""


def style_kind(label: str) -> str:
    """``color`` for Text/Font/Name Color, ``font`` for Font; "" otherwise (e.g. plain ``Color``)."""
    tokens = _tokens(label)
    if tokens & _COLOR_WORDS:
        return STYLE_COLOR if tokens & _STYLE_TARGET_WORDS else ""
    if tokens & {"font", "fonts"} and "size" not in tokens:
        return STYLE_FONT
    return ""


def color_hex(value: Any) -> str:
    """``#RRGGBB`` written in an order value (``Red (#E30200)``), else ""."""
    match = _HEX_RE.search(str(value or ""))
    if not match:
        return ""
    digits = match.group(1).upper()
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return "#" + digits


def normalize_hex(value: Any) -> str:
    """A bare or #-prefixed 3/6 digit hex colour -> ``#RRGGBB``; "" when invalid."""
    text = str(value or "").strip()
    return color_hex(text if text.startswith("#") else "#" + text) if re.fullmatch(
        r"#?(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})", text) else ""


def layer_role(layer: Mapping[str, Any]) -> str:
    """``year`` when the layer name/text carries a year or since/est, else ``name``."""
    for raw in (layer.get("name"), layer.get("text")):
        text = str(raw or "")
        if _YEAR_RE.search(text) or _tokens(text) & _YEAR_LAYER_WORDS:
            return YEAR
    return NAME


def year_text(template_text: Any, value: Any) -> str:
    """Order year written into the template's year text, keeping its wording.

    Standard templates name the layer ``year`` while its text stays
    ``Since 1989``; only the 4-digit year is replaced (2026-10-14: writing the
    bare order value dropped "Since").  A value that already carries its own
    since/est wording, or a template text without exactly one year, is used
    as-is.
    """
    text, raw = str(template_text or ""), str(value or "").strip()
    if not raw or _tokens(raw) & _YEAR_LAYER_WORDS:
        return raw
    years = list(_YEAR_RE.finditer(text))
    if len(years) != 1:
        return raw
    return (text[:years[0].start()] + raw + text[years[0].end():]).strip()


def _center_x(layer: Mapping[str, Any]) -> float | None:
    bbox = layer.get("bbox") or []
    if len(bbox) == 4 and bbox[2] > bbox[0]:
        return (bbox[0] + bbox[2]) / 2
    return None


def text_layers(template) -> list[dict]:
    """Visible text layers of the main document and editable smart objects."""
    result: list[dict] = []

    def add(layer: Mapping[str, Any], smart_path: list[str]) -> None:
        path = _path(layer.get("path_parts") or layer.get("path"))
        if not path or layer.get("kind") != "type" or not layer.get("visible", True):
            return
        item = {
            "smart_object_path": list(smart_path),
            "target_path": path,
            "name": str(layer.get("name") or path[-1]),
            "text": str(layer.get("text") or ""),
            "bbox": list(layer.get("bbox") or []),
        }
        item["role"] = layer_role(item)
        result.append(item)

    for layer in getattr(template, "all_layers", []) or []:
        add(layer, [])
        info = layer.get("smart_object") or {}
        if layer.get("kind") == "smartobject" and info.get("editable") and layer.get("visible", True):
            smart_path = _path(layer.get("path_parts") or layer.get("path"))
            for child in info.get("layers") or []:
                add(child, smart_path)
    return result


# Designer convention (2026-10-13): a hidden rectangle named after the text
# layer plus "范围" (e.g. "Man's Name文本框范围") marks the area the text may
# fill.  The renderer shrinks/enlarges the replaced text to fit it.
_BOX_MARK_RE = re.compile(r"(?:文本框|文字框|文本|文字)?范围框?|\brange\b", re.IGNORECASE)


def _valid_bbox(bbox: Any) -> bool:
    return isinstance(bbox, (list, tuple)) and len(bbox) == 4 and bbox[2] > bbox[0] and bbox[3] > bbox[1]


def _is_fit_box(layer: Mapping[str, Any]) -> bool:
    return (layer.get("kind") not in ("type", "group") and _valid_bbox(layer.get("bbox"))
            and bool(_BOX_MARK_RE.search(str(layer.get("name") or ""))))


def _box_key(name: Any) -> str:
    text = _BOX_MARK_RE.sub("", str(name or "")).casefold()
    return "".join(re.findall(r"[a-z0-9一-鿿]+", text))


def fit_box(template, smart_object_path: Iterable[str], target_path: Iterable[str]) -> list[int] | None:
    """Range box of one text layer: the box named after it, else the only box around it."""
    smart_path, target = _path(smart_object_path), _path(target_path)
    layers = list(getattr(template, "all_layers", []) or [])
    if smart_path:
        owner = next((layer for layer in layers
                      if _path(layer.get("path_parts") or layer.get("path")) == smart_path), None)
        layers = list(((owner or {}).get("smart_object") or {}).get("layers") or [])
    text = next((layer for layer in layers
                 if layer.get("kind") == "type" and _path(layer.get("path_parts") or layer.get("path")) == target), None)
    if text is None:
        return None
    boxes = [layer for layer in layers if _is_fit_box(layer)]
    key = _box_key(text.get("name") or target[-1])
    named = [box for box in boxes if key and _box_key(box.get("name")) == key]
    if len(named) == 1:
        return [int(v) for v in named[0]["bbox"]]
    if named or not _valid_bbox(text.get("bbox")):
        return None
    left, top, right, bottom = text["bbox"]
    cx, cy = (left + right) / 2, (top + bottom) / 2
    around = [box for box in boxes
              if box["bbox"][0] <= cx <= box["bbox"][2] and box["bbox"][1] <= cy <= box["bbox"][3]]
    return [int(v) for v in around[0]["bbox"]] if len(around) == 1 else None


def layer_label(layer: Mapping[str, Any]) -> str:
    """Human-readable ``smart › path`` form, also accepted back by ``parse_layer_label``."""
    smart = "/".join(_path(layer.get("smart_object_path")))
    target = "/".join(_path(layer.get("target_path")))
    return f"{smart} › {target}" if smart else target


def parse_layer_label(text: str) -> tuple[list[str], list[str]]:
    raw = str(text or "")
    if "›" in raw:
        smart, target = raw.split("›", 1)
        return _path(smart.strip()), _path(target.strip())
    return [], _path(raw.strip())


def _man_on_right(template) -> bool:
    """True only when the PSD clearly places Man素材 right of Woman素材."""
    centers: dict[str, float] = {}
    for layer in getattr(template, "all_layers", []) or []:
        tokens = _tokens(layer.get("name"))
        side = "woman" if "woman" in tokens else "man" if "man" in tokens else ""
        center = _center_x(layer)
        if side and center is not None and layer.get("kind") == "group" and side not in centers:
            centers[side] = center
    return len(centers) == 2 and centers["man"] > centers["woman"]


def _mapping(field: Mapping[str, Any], layer: Mapping[str, Any]) -> dict:
    return {
        "section": str(field.get("section") or ""),
        "source_key": str(field.get("label") or ""),
        "field_key": field_key(field.get("section", ""), field.get("label", "")),
        "kind": field["kind"],
        "smart_object_path": list(layer.get("smart_object_path") or []),
        "target_path": list(layer.get("target_path") or []),
        "confidence": 1.0,
        "source": "local",
    }


def text_fields(fields: Iterable[Any], extra_labels: Iterable[str] = ()) -> list[dict]:
    """Unique name/year fields from parsed fields or {section,label} dicts.

    Labels in ``extra_labels`` (normalized; fields the profile maps as text)
    are included as kind ``text`` when they are not name/year fields.
    """
    extra = {_norm_label(label) for label in extra_labels}
    result: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in fields:
        if isinstance(item, Mapping):
            section = str(item.get("section") or item.get("section_identity") or "")
            label = str(item.get("label") or item.get("source_key") or "")
            value = str(item.get("value") or "")
        else:
            section, label, value = str(item.section), str(item.label), str(item.value)
        kind = classify_field(label)
        if not kind and _norm_label(label) in extra and not style_kind(label):
            kind = TEXT
        identity = (section.strip(), _norm_label(label))
        if not kind or identity in seen:
            continue
        seen.add(identity)
        result.append({"section": section.strip(), "label": label.strip(), "kind": kind, "value": value.strip()})
    return result


def local_mappings(template, fields: Iterable[Any]) -> tuple[list[dict], list[dict]]:
    """Map name/year fields by the rules above; return (mappings, unresolved fields)."""
    wanted = text_fields(fields)
    layers = text_layers(template)
    if not wanted or not layers:
        # No text layers: nothing to map; compile time warns instead.
        return [], []
    mappings: list[dict] = []
    unresolved: list[dict] = []

    years = [item for item in wanted if item["kind"] == YEAR]
    year_layers = [layer for layer in layers if layer["role"] == YEAR]
    if years:
        if len(years) == 1 and len(year_layers) == 1:
            mappings.append(_mapping(years[0], year_layers[0]))
        else:
            unresolved.extend(years)

    men = [item for item in wanted if item["kind"] == NAME_MAN]
    women = [item for item in wanted if item["kind"] == NAME_WOMAN]
    generic = [item for item in wanted if item["kind"] == NAME]
    name_layers = [layer for layer in layers if layer["role"] == NAME]
    if men or women:
        pair = None
        if len(men) <= 1 and len(women) <= 1 and not generic and len(name_layers) == 2:
            left, right = sorted(name_layers, key=lambda layer: _center_x(layer) or 0.0)
            separated = (
                _center_x(left) is not None and _center_x(right) is not None
                and left["bbox"][2] <= right["bbox"][0]
            )
            if separated:
                pair = (right, left) if _man_on_right(template) else (left, right)
        if pair:
            if men:
                mappings.append(_mapping(men[0], pair[0]))
            if women:
                mappings.append(_mapping(women[0], pair[1]))
        else:
            unresolved.extend([*men, *women, *generic])
    elif generic:
        if len(generic) == 1 and len(name_layers) == 1:
            mappings.append(_mapping(generic[0], name_layers[0]))
        else:
            unresolved.extend(generic)
    return mappings, unresolved


def _find_mapping(mappings: Iterable[Mapping[str, Any]], field: Mapping[str, Any]) -> Mapping[str, Any] | None:
    key = field_key(field["section"], field["label"])
    label = _norm_label(field["label"])
    by_label = None
    for mapping in mappings:
        if str(mapping.get("field_key") or "") == key:
            return mapping
        if by_label is None and _norm_label(mapping.get("source_key")) == label:
            by_label = mapping
    return by_label


def _is_ignored(ignored: Iterable[str], field: Mapping[str, Any]) -> bool:
    key = field_key(field["section"], field["label"])
    label = _norm_label(field["label"])
    return any(str(item) == key or _norm_label(item) == label for item in ignored or [])


def check_mapping_roles(mappings: Iterable[Mapping[str, Any]], template) -> list[str]:
    """Reject a year written into a name layer and vice versa (used at review time)."""
    layers = {
        (tuple(layer["smart_object_path"]), tuple(layer["target_path"])): layer
        for layer in text_layers(template)
    }
    errors: list[str] = []
    used: dict[tuple, str] = {}
    for mapping in mappings:
        label = str(mapping.get("source_key") or "")
        kind = str(mapping.get("kind") or classify_field(label))
        key = (tuple(_path(mapping.get("smart_object_path"))), tuple(_path(mapping.get("target_path"))))
        layer = layers.get(key)
        if layer is None:
            errors.append(f"文字字段「{label}」的目标不是可见文字图层: {layer_label({'smart_object_path': key[0], 'target_path': key[1]})}")
            continue
        if kind == YEAR and layer["role"] != YEAR:
            errors.append(f"年份字段「{label}」不能写入姓名图层「{layer['name']}」")
        elif kind in NAME_KINDS and layer["role"] == YEAR:
            errors.append(f"姓名字段「{label}」不能写入年份图层「{layer['name']}」")
        if key in used:
            errors.append(f"「{used[key]}」和「{label}」写入了同一个文字图层「{layer['name']}」")
        used[key] = label
    return errors


NO_TEXT_LAYERS = "模板没有可编辑的文字图层（姓名/年份是图片），已跳过"


def lacks_text_layers(template, parsed_fields: Iterable[Any]) -> bool:
    """The order has name/year values but the template has no editable text layer."""
    return any(item["value"] for item in text_fields(parsed_fields)) and not text_layers(template)


def resolve_order_text(template, parsed_fields: Iterable[Any], profile: Mapping[str, Any] | None = None) -> dict:
    """Compile and check name/year replacements for one order.

    A profile whose text was reviewed (``text_reviewed`` or non-empty
    ``text_mappings``) is authoritative; otherwise the local rules run on the
    current order.  Returns main/smart replacements, errors, warnings and the
    handled field labels (callers must keep these out of legacy text logic).
    """
    parsed_fields = list(parsed_fields)
    profile = profile or {}
    mapped_labels = [str(item.get("source_key") or "") for item in profile.get("text_mappings") or []]
    fields = [item for item in text_fields(parsed_fields, mapped_labels) if item["value"]]
    layers = text_layers(template)
    by_key = {(tuple(layer["smart_object_path"]), tuple(layer["target_path"])): layer for layer in layers}
    reviewed = bool(profile.get("text_reviewed") or profile.get("text_mappings"))
    local, _ = local_mappings(template, fields)
    if reviewed:
        mappings = list(profile.get("text_mappings") or [])
        ignored = list(profile.get("ignored_fields") or [])
    else:
        mappings, ignored = local, []

    result = {
        "main": [], "smart": {}, "errors": [], "warnings": [],
        "handled_labels": {item["label"] for item in fields},
        "handled_keys": {field_key(item["section"], item["label"]) for item in fields},
        "used_layers": set(),
        "name_layers": set(),
        "styles": {},
    }
    if fields and not layers:
        # 2026-10-13 user decision: such an image would carry the PSD's sample
        # names, so the order is skipped instead of rendered with a warning.
        result["errors"].append(NO_TEXT_LAYERS)
        return result
    choices = "、".join(layer_label(layer) for layer in layers) or "无"
    for item in fields:
        if _is_ignored(ignored, item):
            continue
        mapping = _find_mapping(mappings, item)
        if mapping is None and reviewed:
            # A field the review never saw (e.g. year missing from samples)
            # may still be resolved by the same deterministic rules.
            mapping = _find_mapping(local, item)
        if mapping is None:
            hint = "，需在审核中指定写入的图层（或留空表示不替换）"
            result["errors"].append(
                f"{KIND_LABELS[item['kind']]}字段「{item['label']}」({item['value']}) 无法确定写入哪个文字图层"
                f"（可见文字图层: {choices}）{hint}"
            )
            continue
        key = (tuple(_path(mapping.get("smart_object_path"))), tuple(_path(mapping.get("target_path"))))
        layer = by_key.get(key)
        if layer is None:
            result["errors"].append(
                f"文字字段「{item['label']}」的目标图层不存在或已隐藏: "
                f"{layer_label({'smart_object_path': key[0], 'target_path': key[1]})}"
            )
            continue
        if item["kind"] == YEAR and layer["role"] != YEAR:
            result["errors"].append(f"年份「{item['value']}」不能写入姓名图层「{layer['name']}」")
            continue
        if item["kind"] in NAME_KINDS and layer["role"] == YEAR:
            result["errors"].append(f"姓名「{item['value']}」不能写入年份图层「{layer['name']}」")
            continue
        if key in result["used_layers"]:
            result["errors"].append(f"多个文字字段写入同一个图层「{layer['name']}」")
            continue
        result["used_layers"].add(key)
        value = item["value"]
        if item["kind"] == YEAR:
            value = year_text(layer["text"], value)
        elif item["kind"] in NAME_KINDS:
            result["name_layers"].add(key)
        replacement = {"path": list(key[1]), "value": value}
        if key[0]:
            result["smart"].setdefault(key[0], []).append(replacement)
        else:
            result["main"].append(replacement)
    if fields:
        for key, layer in by_key.items():
            if key not in result["used_layers"]:
                result["warnings"].append(f"文字图层「{layer['name']}」没有对应的订单字段，保持 PSD 原文")
    _resolve_styles(parsed_fields, profile, result)
    return result


def style_fields(parsed_fields: Iterable[Any], profile: Mapping[str, Any] | None = None) -> list[dict]:
    """Unique text style fields of one order, with the profile's style mapping (if any)."""
    mappings = [item for item in (profile or {}).get("style_mappings") or [] if isinstance(item, Mapping)]
    result: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in parsed_fields:
        if isinstance(item, Mapping):
            section, label, value = (str(item.get("section") or ""), str(item.get("label") or ""),
                                     str(item.get("value") or ""))
        else:
            section, label, value = str(item.section), str(item.label), str(item.value)
        identity = (section.strip(), _norm_label(label))
        if identity in seen:
            continue
        field = {"section": section.strip(), "label": label.strip(), "value": value.strip()}
        mapping = _find_mapping(mappings, field)
        style = str((mapping or {}).get("style") or "") or style_kind(label)
        if style not in STYLE_LABELS:
            continue
        seen.add(identity)
        result.append({**field, "style": style, "mapping": mapping})
    return result


def style_value(field: Mapping[str, Any], profile: Mapping[str, Any] | None = None) -> str:
    """Colour as ``#RRGGBB`` (from the value or the profile's ``color_values``) or font name; "" if unknown."""
    value = str(field.get("value") or "").strip()
    if field.get("style") == STYLE_FONT:
        return value
    known = {_norm_label(name): normalize_hex(hex_value)
             for name, hex_value in ((profile or {}).get("color_values") or {}).items()}
    return color_hex(value) or known.get(_norm_label(value), "")


def _resolve_styles(parsed_fields: Iterable[Any], profile: Mapping[str, Any], result: dict) -> None:
    ignored = list(profile.get("ignored_fields") or [])
    for field in style_fields(parsed_fields, profile):
        result["handled_labels"].add(field["label"])
        result["handled_keys"].add(field_key(field["section"], field["label"]))
        if not field["value"] or _is_ignored(ignored, field):
            continue
        name = STYLE_LABELS[field["style"]]
        value = style_value(field, profile)
        if not value:
            result["errors"].append(
                f"{name}字段「{field['label']}」({field['value']}) 没有色值，需要 AI 分析给出色值或在审核中填写"
            )
            continue
        targets = [
            (tuple(_path(target.get("smart_object_path"))), tuple(_path(target.get("target_path"))))
            for target in (field["mapping"] or {}).get("targets") or [] if isinstance(target, Mapping)
        ]
        if targets:
            targets = [key for key in targets if key in result["used_layers"]]
        else:
            targets = sorted(result["name_layers"]) or sorted(result["used_layers"])
        if not targets:
            result["warnings"].append(f"{name}「{field['value']}」没有本单要写入的文字，未应用")
            continue
        for key in targets:
            result["styles"].setdefault(key, {})[field["style"]] = value


__all__ = [
    "KIND_LABELS", "NAME", "NAME_KINDS", "NAME_MAN", "NAME_WOMAN", "NO_TEXT_LAYERS", "NOT_TEXT_CONTENT_WORDS",
    "STYLE_COLOR", "STYLE_FONT", "STYLE_LABELS", "TEXT", "YEAR",
    "check_mapping_roles", "classify_field", "color_hex", "fit_box", "lacks_text_layers", "layer_label",
    "layer_role", "local_mappings", "normalize_hex", "parse_layer_label", "resolve_order_text", "style_fields",
    "style_kind", "style_value", "text_fields", "text_layers", "year_text",
]
