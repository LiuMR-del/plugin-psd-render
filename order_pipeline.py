"""Pure order-to-render planning shared by intake, review, and rendering.

The module deliberately stops at the argument boundary used by :mod:`renderer`.
It does not call an API, Photoshop, or the renderer.  A caller may materialize
``plan["renderer_kwargs"]`` into ``renderer.render`` after handling errors.

The public flow is:

``parse_fields(raw)`` -> ``materialize_fields(row, fields)`` ->
``profile_to_legacy_configs(reviewed)`` -> ``compile_plan(template, row, cfg, template_dir)``.

``cfg`` is the dictionary returned by ``settings.load``.  A profile bundle from
``profile_to_legacy_configs`` is also accepted by ``compile_plan``; this makes
reviewed mappings useful before they are persisted into settings.
"""

from __future__ import annotations

import copy
import json
import os
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

import custom_fields
import excel_data
import image_assets
import settings
import text_rules


_SECTION_SEPARATOR = "::"
# Replaced text may grow to its range box, but never past this factor.
TEXT_BOX_MAX_SCALE = 1.3


def serialize_field_key(section: str, name: str) -> str:
    """Serialize a section/name pair without ambiguity.

    JSON is used instead of a delimiter because both headings and labels are
    user supplied and may themselves contain punctuation.  The result is
    stable across runs and safe as a dictionary key.
    """
    return json.dumps(
        [str(section or "").strip(), str(name or "").strip()],
        ensure_ascii=False,
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class ParsedField:
    """One original customization field, retaining its section and spelling."""

    section: str
    label: str
    value: str
    ordinal: int = 0

    @property
    def field_key(self) -> str:
        return serialize_field_key(self.section, self.label)

    @property
    def name(self) -> str:
        """Alias for callers that call the original label a field name."""
        return self.label


@dataclass
class ParsedFields:
    """Parsed fields plus every encountered heading, including empty headings.

    The object intentionally supports iteration, indexing, and ``len`` like a
    list of ``ParsedField`` objects while retaining ``sections`` for empty
    sections that have no field row.
    """

    fields: list[ParsedField] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)

    def __iter__(self):
        return iter(self.fields)

    def __len__(self) -> int:
        return len(self.fields)

    def __getitem__(self, index):
        return self.fields[index]

    def __bool__(self) -> bool:
        return bool(self.fields or self.sections)

    def keys(self) -> list[str]:
        return [item.field_key for item in self.fields]


def parse_fields(raw: str) -> ParsedFields:
    """Parse raw customization text while retaining section identity.

    A non-empty line without a colon is a heading.  This deliberately keeps
    arbitrary empty headings, rather than recognizing only ``CUSTOMIZE MAN``
    and ``CUSTOMIZE WOMAN``.  A line with a colon and an empty right side is
    also treated as an empty heading.  Field labels and values are stripped of
    transport whitespace but otherwise kept verbatim.  Repeated labels and
    repeated values are never discarded.
    """
    result = ParsedFields()
    current_section = ""
    ordinal = 0
    for raw_line in str(raw or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        positions = [p for p in (line.find(":"), line.find("：")) if p >= 0]
        split_at = min(positions) if positions else -1
        if split_at < 0:
            current_section = line
            if current_section not in result.sections:
                result.sections.append(current_section)
            continue

        label = line[:split_at].strip()
        value = line[split_at + 1 :].strip()
        if not label:
            # Preserve an otherwise nameless heading as well; it is not a
            # usable field but losing it makes review/debugging misleading.
            if value and value not in result.sections:
                result.sections.append(value)
            continue
        if not value:
            if label not in result.sections:
                result.sections.append(label)
            current_section = label
            continue
        ordinal += 1
        result.fields.append(ParsedField(current_section, label, value, ordinal))
    return result


def _field_iter(fields: ParsedFields | Iterable[ParsedField]) -> list[ParsedField]:
    if isinstance(fields, ParsedFields):
        return list(fields.fields)
    return [item for item in fields if isinstance(item, ParsedField)]


def materialize_field_values(
    fields: ParsedFields | Iterable[ParsedField],
    existing: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Build Excel-compatible values from parsed fields without truncation.

    Every section/name pair receives its serialized key.  A bare original
    label is added only when that label is unambiguous across sections.  More
    than one value for a key is joined with newlines, preserving every value
    in the single-value ``ExcelRow.values`` contract.
    """
    parsed = _field_iter(fields)
    values: dict[str, str] = {
        str(key): str(value) for key, value in (existing or {}).items()
    }
    by_key: "OrderedDict[str, list[str]]" = OrderedDict()
    by_label: "OrderedDict[str, list[ParsedField]]" = OrderedDict()
    for item in parsed:
        by_key.setdefault(item.field_key, []).append(item.value)
        by_label.setdefault(item.label, []).append(item)

    for key, entries in by_key.items():
        values[key] = "\n".join(entries)
    for label, entries in by_label.items():
        sections = {item.section for item in entries}
        if len(sections) == 1:
            values[label] = "\n".join(item.value for item in entries)
    return values


def materialize_fields(
    row: excel_data.ExcelRow,
    fields: ParsedFields | Iterable[ParsedField] | None = None,
) -> excel_data.ExcelRow:
    """Materialize parsed fields into ``row.values`` and return the same row."""
    parsed = parse_fields(row.custom_info) if fields is None else fields
    row.values.update(materialize_field_values(parsed, row.values))
    return row


def _path(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part for part in value.replace("\\", "/").split("/") if part]
    return [str(part) for part in value or [] if str(part)]


def _path_key(value: Iterable[Any] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(_path(value))


def _format_text_value(path: list[str], value: Any) -> str:
    text = str(value or "")
    leaf = path[-1].casefold() if path else ""
    if "since" in leaf and text.strip() and not text.strip().casefold().startswith("since"):
        return "SINCE " + text.strip()
    return text


def _format_text_replacements(items: Iterable[dict]) -> list[dict]:
    return [
        {**dict(item), "value": _format_text_value(_path(item.get("path")), item.get("value"))}
        for item in items
    ]


def _profile_mappings(profile: Mapping[str, Any]) -> list[dict]:
    """Read current and historical profile selector keys."""
    result: list[dict] = []
    for key in ("field_mappings", "reviewed_mappings", "mappings", "person_style_mappings", "selector_mappings"):
        raw = profile.get(key) or []
        if isinstance(raw, Mapping):
            raw = list(raw.values())
        if isinstance(raw, list):
            result.extend(item for item in raw if isinstance(item, Mapping))
    # A reviewed mapping is also accepted directly, and a mapping keyed by
    # field_key is accepted for small callers that do not wrap it in a list.
    if not result and (profile.get("field_key") or profile.get("source_key")):
        result.append(dict(profile))
    if not result and profile:
        keyed = []
        for key, value in profile.items():
            if not isinstance(value, Mapping) or not (value.get("parent") or value.get("parent_path")):
                continue
            item = dict(value)
            item.setdefault("field_key", key)
            keyed.append(item)
        result.extend(keyed)
    return result


def _mapping_field_key(item: Mapping[str, Any]) -> str:
    return str(
        item.get("field_key")
        or item.get("source_key")
        or item.get("field")
        or item.get("column")
        or ""
    ).strip()


def _mapping_parent(item: Mapping[str, Any]) -> list[str]:
    return _path(item.get("parent") or item.get("parent_path"))


def _mapping_smart_path(item: Mapping[str, Any]) -> list[str]:
    return _path(item.get("smart_path") or item.get("smart_object_path"))


def _mapping_candidates(item: Mapping[str, Any]) -> list[dict]:
    raw = item.get("candidates")
    if raw is not None:
        if isinstance(raw, Mapping):
            raw = [{"value": value, "path": path} for value, path in raw.items()]
        result = []
        for candidate in raw or []:
            if not isinstance(candidate, Mapping):
                continue
            path = _path(candidate.get("path") or candidate.get("target_path"))
            value = str(candidate.get("value") or "").strip()
            if path and value:
                result.append({"path": path, "value": value})
        return result

    # Historical profile schema: {value_mapping: {value: [path, ...]}}.
    result = []
    for value, paths in (item.get("value_mapping") or {}).items():
        if isinstance(paths, (str, tuple)):
            paths = [paths]
        for path in paths or []:
            normalized = _path(path)
            if normalized and str(value).strip():
                result.append({"path": normalized, "value": str(value).strip()})
    return result


def _append_selector(selectors: list[dict], selector: dict) -> None:
    key = (_path_key(selector.get("parent")), _path_key(selector.get("smart_path")))
    for existing in selectors:
        existing_key = (_path_key(existing.get("parent")), _path_key(existing.get("smart_path")))
        if existing_key != key:
            continue
        if existing.get("column") == selector.get("column"):
            existing["candidates"] = _merge_candidates(
                existing.get("candidates") or [], selector.get("candidates") or []
            )
            existing["hide_values"] = list(dict.fromkeys([
                *(existing.get("hide_values") or []), *(selector.get("hide_values") or [])
            ]))
        return
    selectors.append(selector)


def _merge_candidates(left: list[dict], right: list[dict]) -> list[dict]:
    result: list[dict] = []
    seen: set[tuple[str, ...] | tuple[tuple[str, ...], str]] = set()
    for candidate in [*left, *right]:
        path = _path(candidate.get("path"))
        value = str(candidate.get("value") or "").strip()
        marker = (tuple(path), value)
        if path and value and marker not in seen:
            seen.add(marker)
            result.append({"path": path, "value": value})
    return result


def profile_to_legacy_configs(
    reviewed: Mapping[str, Any],
    existing: Mapping[str, Any] | None = None,
    *,
    template_dir: str = "",
    sku: str = "",
    filename: str = "",
) -> dict:
    """Convert reviewed mappings to the project's selector and alias shapes.

    Explicit reviewed paths are copied exactly.  Existing aliases/configs win
    on collision, so this function never guesses a layer from a field name.
    Historical ``text_mappings`` and ``selector_mappings`` are accepted.  The
    returned bundle contains ``group_config``, per-smart-object configs,
    exact-path text aliases, and image configs copied from ``existing``.
    When identity arguments are supplied, ``settings``-compatible nested
    dictionaries are included under ``group_configs`` and related keys.
    """
    if not isinstance(reviewed, Mapping):
        raise TypeError("reviewed profile must be a mapping")
    base = copy.deepcopy(dict(existing or {}))
    group = copy.deepcopy(base.get("group_config") or {"selectors": []})
    selectors: list[dict] = []
    smart_configs: dict[tuple[str, ...], dict] = {}
    text_aliases: list[dict] = copy.deepcopy(base.get("text_aliases") or [])
    main_aliases: dict[str, str] = copy.deepcopy(base.get("layer_aliases") or {})
    smart_aliases: list[dict] = copy.deepcopy(base.get("smart_object_aliases") or [])
    image_configs: Any = copy.deepcopy(base.get("image_replacement_configs") or [])

    # Preserve prior settings entries when a full settings dictionary is
    # passed.  The selected filename's group configuration is the legacy one.
    if template_dir and sku and filename:
        old_group = settings.get_group_config(base, template_dir, sku, filename)
        group = copy.deepcopy(old_group)
        main_aliases = copy.deepcopy(settings.get_layer_aliases(base, template_dir, sku))
        smart_aliases = copy.deepcopy(settings.get_smart_object_aliases(base, template_dir, sku, filename))
        image_configs = copy.deepcopy(settings.get_image_replacement_configs(base, template_dir, sku, filename))
        for item in settings.get_smart_object_configs(base, template_dir, sku, filename):
            smart_configs[tuple(item["smart_path"])] = copy.deepcopy(item)

    for mapping in _profile_mappings(reviewed):
        field_key = _mapping_field_key(mapping)
        smart_path = _mapping_smart_path(mapping)
        parent = _mapping_parent(mapping)
        candidates = _mapping_candidates(mapping)
        hide_values = [str(value).strip() for value in [
            *(mapping.get("hide_values") or []),
            *(mapping.get("hide_parent_values") or []),
        ] if str(value).strip()]
        if field_key and parent and candidates:
            selector = {
                "parent": parent,
                "enabled": True,
                "column": field_key,
                "candidates": candidates,
                "relative_text_aliases": {},
                "hide_values": hide_values,
                "smart_path": smart_path,
            }
            if smart_path:
                target = smart_configs.setdefault(tuple(smart_path), {"smart_path": smart_path, "selectors": []})
                _append_selector(target["selectors"], selector)
            else:
                _append_selector(selectors, selector)

    # Ordinary text mappings are explicit aliases.  Preserve existing entries
    # and use exact path records so duplicate layer names stay unambiguous.
    for mapping in reviewed.get("text_mappings") or []:
        if not isinstance(mapping, Mapping):
            continue
        field_key = _mapping_field_key(mapping)
        target = _path(mapping.get("target_path") or mapping.get("path"))
        smart_path = _mapping_smart_path(mapping)
        text_path = _path(mapping.get("text_path") or target)
        if not field_key or not text_path:
            continue
        alias_record = {"smart_path": smart_path, "text_path": text_path, "column": field_key}
        if alias_record not in text_aliases:
            text_aliases.append(alias_record)
        if smart_path:
            if alias_record not in smart_aliases:
                smart_aliases.append(alias_record)
        elif target:
            leaf = target[-1]
            main_aliases.setdefault(leaf, field_key)

    group["selectors"] = list(group.get("selectors") or [])
    for selector in selectors:
        _append_selector(group["selectors"], selector)

    bundle = {
        "group_config": group,
        "smart_object_configs": [copy.deepcopy(item) for item in smart_configs.values()],
        "layer_aliases": main_aliases,
        "smart_object_aliases": smart_aliases,
        "text_aliases": text_aliases,
        "image_replacement_configs": image_configs,
        "profile": copy.deepcopy(dict(reviewed)),
    }
    if template_dir and sku and filename:
        bundle["group_configs"] = {template_dir: {sku: {filename: copy.deepcopy(group)}}}
        bundle["layer_aliases_by_identity"] = {template_dir: {sku: main_aliases}}
        bundle["smart_object_configs_by_identity"] = {template_dir: {sku: {filename: bundle["smart_object_configs"]}}}
        bundle["smart_object_aliases_by_identity"] = {template_dir: {sku: {filename: smart_aliases}}}
    return bundle


def _template_layers(template) -> tuple[
    dict[tuple[str, ...], dict],
    dict[tuple[str, ...], dict[tuple[str, ...], dict]],
]:
    main: dict[tuple[str, ...], dict] = {}
    inner: dict[tuple[str, ...], dict[tuple[str, ...], dict]] = {}
    for layer in getattr(template, "all_layers", []) or []:
        parts = _path(layer.get("path_parts") or layer.get("path"))
        if not parts:
            continue
        key = tuple(parts)
        main[key] = layer
        if layer.get("kind") == "smartobject":
            info = layer.get("smart_object") or {}
            if info.get("editable"):
                inner[key] = {
                    tuple(_path(child.get("path_parts") or child.get("path"))): child
                    for child in info.get("layers") or []
                    if _path(child.get("path_parts") or child.get("path"))
                }
    return main, inner


def _bundle_value(cfg: Mapping[str, Any], key: str, default: Any) -> Any:
    return copy.deepcopy(cfg.get(key, default)) if isinstance(cfg, Mapping) else copy.deepcopy(default)


def _identity_config(cfg: Mapping[str, Any], template_dir: str, template, key: str, default: Any) -> Any:
    """Read a settings config or a profile bundle without mutating it."""
    sku = str(getattr(template, "sku", "") or "")
    filename = str(getattr(template, "filename", "") or "")
    raw = cfg.get(key) if isinstance(cfg, Mapping) else None

    # profile_to_legacy_configs returns flat/list values.  settings.load()
    # returns identity-nested dictionaries.  Distinguish those shapes before
    # calling settings helpers, which expect the latter.
    if key == "group_config" and "group_config" in cfg:
        return _bundle_value(cfg, key, default)
    if key == "layer_aliases" and isinstance(raw, Mapping):
        # A flat alias map is {layer_name: column}; settings uses
        # {template_dir: {sku: {layer_name: column}}}.
        if not any(isinstance(value, Mapping) for value in raw.values()):
            return _bundle_value(cfg, key, default)
    if key == "smart_object_aliases" and isinstance(raw, list):
        return _bundle_value(cfg, key, default)
    if key == "smart_object_configs" and isinstance(raw, list):
        return _bundle_value(cfg, key, default)
    if key == "image_replacement_configs" and isinstance(raw, list):
        return _bundle_value(cfg, key, default)

    if key == "group_config":
        return settings.get_group_config(dict(cfg), template_dir, sku, filename)
    if key == "layer_aliases":
        return settings.get_layer_aliases(dict(cfg), template_dir, sku)
    if key == "smart_object_aliases":
        return settings.get_smart_object_aliases(dict(cfg), template_dir, sku, filename)
    if key == "smart_object_configs":
        return settings.get_smart_object_configs(dict(cfg), template_dir, sku, filename)
    if key == "image_replacement_configs":
        return settings.get_image_replacement_configs(dict(cfg), template_dir, sku, filename)
    return copy.deepcopy(default)


def _as_selectors(config: Mapping[str, Any] | None) -> list[dict]:
    result = []
    for raw in (config or {}).get("selectors", []) if isinstance(config, Mapping) else []:
        if not isinstance(raw, Mapping):
            continue
        item = {
            "parent": _path(raw.get("parent")),
            "enabled": bool(raw.get("enabled", True)),
            "column": str(raw.get("column") or "").strip(),
            "candidates": _merge_candidates([], list(raw.get("candidates") or [])),
            "relative_text_aliases": {
                str(k): str(v).strip() for k, v in (raw.get("relative_text_aliases") or {}).items() if str(v).strip()
            },
            "hide_values": [str(v).strip() for v in raw.get("hide_values") or [] if str(v).strip()],
        }
        if item["enabled"] and item["column"] and item["candidates"]:
            result.append(item)
    return result


def _selector_paths(selectors: Iterable[dict]) -> Iterable[list[str]]:
    for selector in selectors:
        if selector.get("parent"):
            yield selector["parent"]
        for candidate in selector.get("candidates") or []:
            yield _path(candidate.get("path"))


def _validate_selector_paths(
    selectors: Iterable[dict],
    valid: set[tuple[str, ...]],
    errors: list[str],
    *,
    context: str = "",
) -> None:
    """Validate configured selector targets even when this row has no value."""
    for selector in selectors:
        parent = _path(selector.get("parent"))
        if parent and tuple(parent) not in valid:
            errors.append(f"{context}切换父级路径不存在: {'/'.join(parent)}")
        for candidate in selector.get("candidates") or []:
            path = _path(candidate.get("path"))
            if path and tuple(path) not in valid:
                errors.append(f"{context}切换候选路径不存在: {'/'.join(path)}")


def _ancestors(path: list[str]) -> list[list[str]]:
    return [path[:index] for index in range(1, len(path) + 1)]


def _name_tokens(value: str) -> set[str]:
    """Extract comparison tokens from a human/configured layer name."""
    import re

    text = str(value or "").casefold()
    text = re.sub(r"^\\s*\\d+\\s*[-_.、：:]?\\s*", "", text)
    return {token for token in re.findall(r"[a-z0-9]+", text) if token not in {"the", "and"}}


def _infer_hide_selectors(
    selectors: list[dict],
    values: Mapping[str, str],
    valid: set[tuple[str, ...]],
) -> list[dict]:
    """Add deterministic parent-hide rules for explicit No/隐藏 values.

    Some historical AI drafts omitted fields whose only value was ``No``.
    Hiding is still unambiguous: match the field label to a real PSD group
    name (for example ``Man's Glasses`` -> ``5-Man's Glasses``) and hide that
    whole group. No candidate selection is inferred here.
    """
    hide_values = {"no", "none", "null", "无", "隐藏", "hide", "hidden"}
    configured_columns = {str(item.get("column") or "").strip() for item in selectors}
    inferred: list[dict] = []
    for column, raw_value in values.items():
        value = str(raw_value or "").strip()
        if not column or value.casefold() not in hide_values or column in configured_columns:
            continue
        field_tokens = _name_tokens(column)
        if not field_tokens:
            continue
        matches: list[tuple[int, int, tuple[str, ...]]] = []
        for path in valid:
            if not path:
                continue
            tokens = _name_tokens(path[-1])
            overlap = len(field_tokens & tokens)
            if overlap and any(token in tokens for token in ("hair", "glasses", "beard", "drink", "pants", "top", "chair")):
                matches.append((overlap, len(path), path))
        if not matches:
            continue
        # Select the shortest matching group at the highest semantic overlap;
        # descendants such as Glasses_01 do not contain the field tokens.
        _, _, path = max(matches, key=lambda item: (item[0], -item[1]))
        inferred.append({
            "parent": list(path),
            "enabled": True,
            "column": column,
            "candidates": [{"path": list(path), "value": "__inferred_hide__"}],
            "relative_text_aliases": {},
            "hide_values": [value],
        })
    return inferred


def _selector_scope(selector: Mapping[str, Any], valid: set[tuple[str, ...]]) -> list[str]:
    """Find the real mutually-exclusive container for a selector.

    Reviewed mappings sometimes store a selected branch as ``parent`` (for
    example ``7-Man's Top/Sweater`` or ``Hair Color/Brown``).  Hiding only
    that branch leaves sibling branches visible.  Prefer an ancestor whose
    name matches the business field; otherwise retain the configured parent.
    """
    parent = _path(selector.get("parent"))
    if not parent:
        return parent
    column = str(selector.get("column") or "")
    # A suffix such as ``Hair-Brown`` identifies the Hair family, not the
    # selected Brown branch itself.
    base = column.split("-", 1)[0].split("–", 1)[0].strip()
    field_tokens = _name_tokens(base)
    candidates: list[tuple[int, int, list[str]]] = []
    for length in range(1, len(parent) + 1):
        path = parent[:length]
        key = tuple(path)
        if key not in valid:
            continue
        overlap = len(field_tokens & _name_tokens(path[-1]))
        if overlap:
            candidates.append((overlap, length, path))
    if candidates:
        # Prefer the most specific matching ancestor; overlap breaks ties.
        _, _, selected = max(candidates, key=lambda item: (item[1], item[0]))
        return selected
    return parent


def _norm_material(value: Any) -> str:
    return "".join(ch.casefold() for ch in str(value or "") if ch.isalnum())


def _dynamic_selector_candidates(value: str, scope: list[str], valid: set[tuple[str, ...]]) -> list[dict]:
    target = _norm_material(value)
    if not target or target in {"no", "none", "null", "无", "隐藏", "hide", "hidden"}:
        return []
    # Two-level convention (Hair Color=Brown + Hair-Brown=Brown_07):
    # - a numbered value (Brown_07) addresses exactly one leaf; only the
    #   leaf's own name counts, never an ancestor branch name.
    # - an unnumbered value (Brown) addresses the branch level only; it must
    #   not light up every leaf below, or all siblings stack onto the canvas.
    #   The numbered sibling field then opens the single leaf it names.
    import re
    numbered = bool(re.search(r"\d", target))
    leaf_hits: list[dict] = []
    branch_hits: list[dict] = []
    for path in sorted(valid):
        if len(path) <= len(scope) or path[:len(scope)] != tuple(scope):
            continue
        own_names = {_norm_material(path[-1])}
        if "_" in str(path[-1]):
            own_names.add(_norm_material(str(path[-1]).split("_", 1)[1]))
        own_names.discard("")
        if any(target == name for name in own_names):
            # The leaf's own name matches: a direct leaf hit at any depth
            # (Drink_03 hangs directly under its group; Hair_Brown_07 hangs
            # under its color branch). Ancestor branch names never count here.
            leaf_hits.append({"path": list(path), "value": value})
            continue
        mid = [_norm_material(part) for part in path[len(scope):-1]]
        if any(target == name for name in mid):
            branch_hits.append({"path": list(path[:len(scope) + 1]), "value": value})
            continue
        aliases = set(own_names) | set(mid)
        if any(target in name or name in target for name in aliases if name):
            branch_hits.append({"path": list(path[:len(scope) + 1]), "value": value})
    if numbered:
        if leaf_hits:
            best = max(len(item["path"]) for item in leaf_hits)
            return [item for item in leaf_hits if len(item["path"]) == best]
        return []
    seen: set[tuple[str, ...]] = set()
    unique: list[dict] = []
    for item in branch_hits:
        key = tuple(item["path"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _selector_actions(selectors: list[dict], values: Mapping[str, str], valid: set[tuple[str, ...]], errors: list[str], *, context: str = "") -> tuple[list[dict], list[dict]]:
    """Compile selectors as one deterministic final visibility state."""
    final_state: OrderedDict[tuple[str, ...], bool] = OrderedDict()
    blocked_scopes: list[tuple[str, ...]] = []
    replacements: list[dict] = []
    for selector in selectors:
        column = selector["column"]
        value = str(values.get(column, "") or "").strip()
        if not value:
            continue
        parent = selector["parent"]
        candidates = selector["candidates"]
        hide_values = {str(item).strip().casefold() for item in selector.get("hide_values") or []}
        hide_values.update({"no", "none", "null", "无", "隐藏", "hide", "hidden"})
        parent_key = tuple(parent)
        scope = _selector_scope(selector, valid)
        scope_key = tuple(scope)
        if scope_key not in valid:
            errors.append(f"{context}切换互斥父级路径不存在: {'/'.join(scope)}")
            continue

        # Every selector owns the complete real subtree of its mutual-
        # exclusion container, including branches not present in the AI
        # profile.  This is what removes PSD defaults and sibling branches.
        descendants = sorted(
            path for path in valid
            if len(path) > len(scope_key) and path[:len(scope_key)] == scope_key
        )
        for path in descendants:
            final_state[path] = False
        if value.casefold() in hide_values:
            final_state[scope_key] = False
            blocked_scopes.append(scope_key)
            continue

        if any(scope_key[:len(blocked)] == blocked for blocked in blocked_scopes):
            # A parent No/隐藏 decision wins over stale child values in the
            # same material tree; never reopen a descendant afterwards.
            continue

        matches = [candidate for candidate in candidates if str(candidate["value"]).strip().casefold() == value.casefold()]
        if not matches:
            matches = _dynamic_selector_candidates(value, scope, valid)
        if not matches:
            errors.append(f"{context}切换字段「{column}」的值「{value}」没有匹配候选")
            continue
        if parent_key not in valid:
            errors.append(f"{context}切换父级路径不存在: {'/'.join(parent)}")
        for candidate in matches:
            candidate_path = _path(candidate["path"])
            if tuple(candidate_path) not in valid:
                errors.append(f"{context}切换候选路径不存在: {'/'.join(candidate_path)}")
                continue
            for ancestor in _ancestors(candidate_path):
                if tuple(ancestor) in valid:
                    final_state[tuple(ancestor)] = True
            final_state[tuple(candidate_path)] = True
            for relative, alias_column in selector.get("relative_text_aliases", {}).items():
                rel = _path(relative)
                text_value = str(values.get(alias_column, "") or "")
                target = candidate_path + rel
                if text_value and tuple(target) in valid:
                    replacements.append({"path": target, "value": text_value})
                elif text_value and tuple(target) not in valid:
                    errors.append(f"{context}候选文字路径不存在: {'/'.join(target)}")
    return ([{"path": list(path), "visible": visible} for path, visible in final_state.items()], replacements)


def _merge_path_replacements(*groups: Iterable[dict]) -> list[dict]:
    result: OrderedDict[tuple[str, ...], dict] = OrderedDict()
    for group in groups:
        for item in group:
            path = _path(item.get("path"))
            if path:
                result[tuple(path)] = {"path": path, "value": item.get("value", "")}
    return list(result.values())


def _smart_id(layer: Mapping[str, Any], path: list[str]) -> str:
    info = layer.get("smart_object") or {}
    return str(info.get("id") or "path:" + "/".join(path))


def _merge_smart_tasks(tasks: list[dict], errors: list[str]) -> list[dict]:
    by_id: OrderedDict[str, dict] = OrderedDict()
    for task in tasks:
        object_id = str(task.get("smartObjectId") or "")
        if not object_id:
            continue
        normalized = {
            "smartObjectId": object_id,
            "smartObjectPath": _path(task.get("smartObjectPath")),
            "visibilityActions": list(task.get("visibilityActions") or []),
            "pathReplacements": list(task.get("pathReplacements") or []),
        }
        existing = by_id.get(object_id)
        if existing is None:
            by_id[object_id] = normalized
            continue
        for key, value_key in (("visibilityActions", "visible"), ("pathReplacements", "value")):
            merged: OrderedDict[tuple[str, ...], dict] = OrderedDict()
            for item in [*existing[key], *normalized[key]]:
                path = _path_key(item.get("path"))
                if not path:
                    continue
                previous = merged.get(path)
                if previous is not None and previous.get(value_key) != item.get(value_key):
                    errors.append(
                        "共享智能对象配置冲突: " + object_id + " · " + "/".join(path)
                    )
                # Last compiled state wins, while preserving one action per
                # path. Photoshop therefore cannot resurrect a hidden sibling
                # merely because another task visited the shared object later.
                merged[path] = dict(item)
            existing[key] = list(merged.values())
    return [task for task in by_id.values() if task["visibilityActions"] or task["pathReplacements"]]


def _exact_alias(aliases: Iterable[Mapping[str, Any]], smart_path: list[str], text_path: list[str]) -> str:
    for item in aliases or []:
        if _path_key(item.get("smart_path")) == tuple(smart_path) and _path_key(item.get("text_path")) == tuple(text_path):
            return str(item.get("column") or "").strip()
    return ""


def _text_aliases_for(bundle: Any, smart_path: list[str], text_path: list[str]) -> str:
    return _exact_alias(bundle if isinstance(bundle, list) else [], smart_path, text_path)


# ---------- field-level person-material rules ----------
#
# Person-material PSDs follow one naming convention:
#   {Man素材|Woman素材}/.../{N-Category}/[branch/]{technical leaf}
# e.g. ``Man素材/组 1/3-Man's Hair Color/Gray/Hair_Gray_10``.  An order field is
# bound to its category group by section side + label tokens, and its value is
# resolved by layer name inside that group.  This does not depend on values
# seen in earlier sample orders, so a new hair color or beard number needs no
# re-review.  Sample-learned selectors remain a fallback for groups whose
# values cannot be resolved by name.

_MATERIAL_GROUP_RE = re.compile(r"^\s*\d+\s*[-_.]")
_MATERIAL_STOP_TOKENS = {
    "man", "woman", "male", "female", "s", "the", "and", "a", "of", "your",
    "choose", "select", "customize", "custom",
}
_HIDE_VALUES = {"no", "none", "null", "无", "隐藏", "hide", "hidden"}


def _material_tokens(value: Any) -> set[str]:
    text = str(value or "").casefold().replace("’", "'")
    return {
        token for token in re.findall(r"[a-z0-9]+", text)
        if token not in _MATERIAL_STOP_TOKENS and not token.isdigit()
    }


def _material_side(value: Any) -> str:
    tokens = set(re.findall(r"[a-z0-9]+", str(value or "").casefold()))
    if tokens & {"woman", "female"}:
        return "woman"
    if tokens & {"man", "male"}:
        return "man"
    return ""


def _material_names(name: str) -> set[str]:
    """Normalized names a layer answers to: ``Hair_Gray_10`` -> hairgray10, gray10."""
    names = {_norm_material(name)}
    if "_" in name:
        names.add(_norm_material(name.split("_", 1)[1]))
    names.discard("")
    return names


def _material_scopes(layers: Mapping[tuple[str, ...], dict]) -> list[tuple[str, tuple[str, ...]]]:
    """Return (side, path) for outermost numbered category groups under a person root."""
    scopes: list[tuple[str, tuple[str, ...]]] = []
    for path, layer in layers.items():
        if not path or layer.get("kind") != "group" or not _MATERIAL_GROUP_RE.match(path[-1]):
            continue
        if any(_MATERIAL_GROUP_RE.match(part) for part in path[:-1]):
            continue
        side = next((s for s in (_material_side(part) for part in path) if s), "")
        if side:
            scopes.append((side, path))
    return scopes


def _field_scope(item: ParsedField, scopes: list[tuple[str, tuple[str, ...]]]) -> tuple[str, ...] | None:
    """Bind ``CUSTOMIZE MAN · Man's Hair-Gray`` to ``3-Man's Hair Color``; unique match only."""
    side = _material_side(item.section)
    if not side:
        return None
    # ``Hair-Gray`` names the Hair family; the suffix is the selected branch.
    tokens = _material_tokens(item.label.split("-", 1)[0])
    if not tokens:
        return None
    scored = [
        (len(tokens & _material_tokens(path[-1])), path)
        for scope_side, path in scopes if scope_side == side
    ]
    best = max((score for score, _ in scored), default=0)
    hits = [path for score, path in scored if score and score == best]
    return hits[0] if len(hits) == 1 else None


def _resolve_material(value: str, scope: tuple[str, ...], layers: Mapping[tuple[str, ...], dict]) -> tuple[str, ...] | None:
    """Resolve one value to one layer inside ``scope`` by exact normalized name.

    A direct child wins (``Gray`` branch, ``Brown`` chair, ``Aged Man 002``);
    otherwise the unique deeper leaf (``Gray_10`` -> ``Gray/Hair_Gray_10``).
    Anything ambiguous returns None rather than guessing.
    """
    target = _norm_material(value)
    if not target:
        return None
    under = [path for path in layers if len(path) > len(scope) and path[:len(scope)] == scope]
    direct = [path for path in under if len(path) == len(scope) + 1 and target in _material_names(path[-1])]
    if direct:
        return direct[0] if len(direct) == 1 else None
    deep = [path for path in under if target in _material_names(path[-1])]
    leaves = [path for path in deep if layers[path].get("kind") != "group"] or deep
    return leaves[0] if len(leaves) == 1 else None


def _material_plan(parsed: ParsedFields, layers: Mapping[tuple[str, ...], dict]) -> dict:
    """Compile field-level material rules for one layer tree.

    Returns ``by_scope`` (every category group touched by the order, with its
    fields and expected outcome), ``managed`` (groups fully resolved here) and
    ``actions`` (final visibility for managed groups).  Several fields may
    target one group (``Hair Color=Gray`` + ``Hair-Gray=Gray_10``); their
    targets are combined and everything else in the group is hidden.
    """
    scopes = _material_scopes(layers)
    by_scope: OrderedDict[tuple[str, ...], dict] = OrderedDict()
    if not scopes:
        return {"by_scope": by_scope, "managed": [], "actions": OrderedDict()}
    for item in parsed:
        scope = _field_scope(item, scopes)
        if scope is None:
            continue
        entry = by_scope.setdefault(scope, {"fields": [], "hide": False, "targets": [], "unresolved": []})
        entry["fields"].append(f"{item.label}={item.value}")
        if item.value.strip().casefold() in _HIDE_VALUES:
            entry["hide"] = True
            continue
        target = _resolve_material(item.value, scope, layers)
        if target is None:
            entry["unresolved"].append(f"{item.label}={item.value}")
        else:
            entry["targets"].append(target)

    actions: OrderedDict[tuple[str, ...], bool] = OrderedDict()
    managed: list[tuple[str, ...]] = []
    for scope, entry in by_scope.items():
        if entry["unresolved"]:
            continue
        managed.append(scope)
        for path in sorted(path for path in layers if len(path) > len(scope) and path[:len(scope)] == scope):
            actions[path] = False
        if entry["hide"]:
            # A No/隐藏 value wins over any other field in the same group.
            actions[scope] = False
            continue
        for target in entry["targets"]:
            for length in range(1, len(target) + 1):
                if target[:length] in layers:
                    actions[target[:length]] = True
    return {"by_scope": by_scope, "managed": managed, "actions": actions}


def material_field_states(template, parsed: ParsedFields | Iterable[ParsedField]) -> dict[tuple[str, str], bool]:
    """(section, label) -> True when the field-level material rule resolves the
    value (or a No/hide value), False when the field binds to a material group
    but its value matches no layer name. Fields of no material group are absent."""
    main, inner = _template_layers(template)
    states: dict[tuple[str, str], bool] = {}
    for layers in (main, *inner.values()):
        scopes = _material_scopes(layers)
        if not scopes:
            continue
        for item in _field_iter(parsed):
            scope = _field_scope(item, scopes)
            if scope is None:
                continue
            key = (item.section.strip(), " ".join(item.label.split()).casefold())
            resolved = (item.value.strip().casefold() in _HIDE_VALUES
                        or _resolve_material(item.value, scope, layers) is not None)
            states[key] = states.get(key, False) or resolved
    return states


def _selector_in_scopes(selector: Mapping[str, Any], scopes: list[tuple[str, ...]], valid: set[tuple[str, ...]]) -> bool:
    """True when a sample-learned selector only touches groups the field rules own."""
    if not scopes:
        return False
    paths = [tuple(_selector_scope(selector, valid)), tuple(_path(selector.get("parent")))]
    paths.extend(tuple(_path(candidate.get("path"))) for candidate in selector.get("candidates") or [])
    paths = [path for path in paths if path]
    return bool(paths) and all(
        any(path[:len(scope)] == scope for scope in scopes) for path in paths
    )


def _overlay_actions(base: list[dict], overlay: Mapping[tuple[str, ...], bool]) -> list[dict]:
    merged: OrderedDict[tuple[str, ...], bool] = OrderedDict(
        (tuple(_path(item.get("path"))), bool(item.get("visible"))) for item in base
    )
    merged.update(overlay)
    return [{"path": list(path), "visible": visible} for path, visible in merged.items()]


def _check_material_scopes(
    by_scope: Mapping[tuple[str, ...], dict],
    actions: list[dict],
    layers: Mapping[tuple[str, ...], dict],
    errors: list[str],
    *,
    context: str = "",
) -> None:
    """Every touched group must end with exactly one visible material (0 for No).

    This catches a selected branch without a leaf (bald hair), stale PSD
    defaults left on, and stacked siblings before Photoshop is started.
    """
    state = {tuple(_path(item.get("path"))): bool(item.get("visible")) for item in actions}

    def visible(path: tuple[str, ...]) -> bool:
        return all(
            state.get(path[:length], bool((layers.get(path[:length]) or {}).get("visible", True)))
            for length in range(1, len(path) + 1)
        )

    for scope, entry in by_scope.items():
        shown = [
            path for path, layer in layers.items()
            if len(path) > len(scope) and path[:len(scope)] == scope
            and layer.get("kind") != "group" and visible(path)
        ]
        expected = 0 if entry["hide"] else 1
        untouched = entry["unresolved"] and not any(
            len(path) > len(scope) and path[:len(scope)] == scope for path in state
        )
        if len(shown) == expected and not untouched:
            continue
        detail = "、".join("/".join(path[len(scope):]) for path in shown) or "无"
        hint = f"；无法按图层名匹配: {'、'.join(entry['unresolved'])}" if entry["unresolved"] else ""
        errors.append(
            f"{context}素材组「{scope[-1]}」应显示 {expected} 个素材，实际 {len(shown)} 个"
            f"（订单: {'；'.join(entry['fields'])}；显示: {detail}{hint}）"
        )


MATERIAL_CONTAINER_HIDDEN = "模板中的人物智能对象「{names}」是隐藏的，人物定制不会出现在图上；请在 PSD 中显示它（并删除盖在上面的整幅像素层）"


def _check_material_containers(
    holders: list[tuple[str, ...]],
    actions: list[dict],
    layers: Mapping[tuple[str, ...], dict],
    errors: list[str],
) -> None:
    """At least one smart object holding the order's material groups must be shown.

    The per-group check runs inside the smart object and cannot see that the
    object itself is hidden in the main document (a stamped full-canvas copy
    then shows the PSD default people).  Hidden duplicates next to a visible
    copy are normal and pass.
    """
    if not holders:
        return
    state = {tuple(_path(item.get("path"))): bool(item.get("visible")) for item in actions}

    def visible(path: tuple[str, ...]) -> bool:
        return all(
            state.get(path[:length], bool((layers.get(path[:length]) or {}).get("visible", True)))
            for length in range(1, len(path) + 1)
        )

    if not any(visible(path) for path in holders):
        errors.append(MATERIAL_CONTAINER_HIDDEN.format(names="、".join("/".join(path) for path in holders)))


def _profile_signature_error(cfg: Mapping[str, Any], template, errors: list[str], strict: bool) -> None:
    profile = cfg.get("profile") if isinstance(cfg, Mapping) else None
    if not isinstance(profile, Mapping):
        return
    status = str(profile.get("status") or "")
    if strict and status and status != "approved":
        errors.append("模板档案尚未审核通过")
    expected = str(profile.get("structure_signature") or "")
    actual = str(getattr(template, "structure_signature", "") or "")
    if expected and actual and expected != actual:
        errors.append("PSD 图层结构签名已变化")


def _attach_fit_boxes(template, smart_path: list[str], replacements: list[dict],
                      name_layers: Iterable[tuple] = ()) -> None:
    """Add the hidden range box (``fitBox``) of each replaced text layer, if any.

    Name layers get ``syncGroup: "name"``: the renderer scales all of them in
    one document by the smallest fit, so the names stay the same size
    (2026-10-14 user request).
    """
    names = {(tuple(sp), tuple(tp)) for sp, tp in name_layers}
    for item in replacements:
        box = text_rules.fit_box(template, smart_path, item.get("path"))
        if box:
            left, top, right, bottom = box
            item["fitBox"] = {"left": left, "top": top, "right": right, "bottom": bottom,
                              "maxScale": TEXT_BOX_MAX_SCALE}
            if (tuple(smart_path), _path_key(item.get("path"))) in names:
                item["fitBox"]["syncGroup"] = "name"


def _attach_text_styles(smart_path: list[str], replacements: list[dict],
                        styles: Mapping[tuple, Mapping[str, str]]) -> None:
    """Add the order's text colour (``color``, ``#RRGGBB``) / font family (``font``).

    Optional ``pathReplacements`` keys (2026-10-15 user request); the renderer
    applies them after writing the text and before fitting it to its box.
    """
    for item in replacements:
        style = styles.get((tuple(smart_path), _path_key(item.get("path")))) or {}
        for key in (text_rules.STYLE_COLOR, text_rules.STYLE_FONT):
            if style.get(key):
                item[key] = style[key]


def compile_plan(template, row: excel_data.ExcelRow, cfg: Mapping[str, Any], template_dir: str, *, strict: bool = True) -> dict:
    """Compile one deterministic renderer plan from a template and order row.

    The return value has the same four keyword payloads consumed by
    ``renderer.render`` plus ``errors`` and ``ok``.  Unknown selector values,
    missing configured paths, invalid image mappings, signature mismatches,
    and shared smart-object conflicts are reported as errors.  No external
    process is started.
    """
    errors: list[str] = []
    cfg = cfg if isinstance(cfg, Mapping) else {}
    _profile_signature_error(cfg, template, errors, strict)
    parsed = parse_fields(getattr(row, "custom_info", ""))
    materialize_fields(row, parsed)
    values = {str(key): str(value or "") for key, value in getattr(row, "values", {}).items()}
    # Order text sometimes carries doubled spaces (``Man's  Hair Color``);
    # configured columns use single spaces.  Expose both spellings.
    for key, value in list(values.items()):
        values.setdefault(" ".join(key.split()), value)
    warnings: list[str] = []
    main_layers, inner_layers = _template_layers(template)
    main_valid = set(main_layers)
    aliases = _identity_config(cfg, template_dir, template, "layer_aliases", {}) or {}
    text_aliases = _bundle_value(cfg, "text_aliases", [])
    group = _identity_config(cfg, template_dir, template, "group_config", {"selectors": []}) or {"selectors": []}
    selectors = _as_selectors(group)
    # Rules stored in the template-library profile travel with the library
    # (config.json does not), so they are applied here as well.
    profile_rules = profile_to_legacy_configs(cfg["profile"]) if isinstance(cfg.get("profile"), Mapping) else {}
    for selector in _as_selectors(profile_rules.get("group_config") or {}):
        _append_selector(selectors, selector)
    selectors.extend(_infer_hide_selectors(selectors, values, main_valid))
    _validate_selector_paths(selectors, main_valid, errors)
    main_material = _material_plan(parsed, main_layers)
    selectors = [item for item in selectors if not _selector_in_scopes(item, main_material["managed"], main_valid)]
    main_visibility, main_selector_replacements = _selector_actions(selectors, values, main_valid, errors)
    main_visibility = _overlay_actions(main_visibility, main_material["actions"])
    _check_material_scopes(main_material["by_scope"], main_visibility, main_layers, errors)

    variant_field = settings.get_variant_field(dict(cfg), template_dir, str(getattr(template, "sku", "") or "")) if "variant_fields" in cfg else ""
    managed_main: set[tuple[str, ...]] = set()
    for selector in selectors:
        for candidate in selector.get("candidates") or []:
            for rel in (selector.get("relative_text_aliases") or {}):
                rel_path = _path(rel)
                if rel_path:
                    managed_main.add(tuple(_path(candidate.get("path")) + rel_path))

    # Name/year fields go through text_rules only (reviewed profile mapping,
    # else local rules) and are checked there; legacy aliases below handle
    # the remaining columns and must never touch those fields or layers.
    profile = cfg.get("profile") if isinstance(cfg.get("profile"), Mapping) else None
    text_plan = text_rules.resolve_order_text(template, parsed, profile)
    errors.extend(text_plan["errors"])
    warnings.extend(text_plan["warnings"])
    rule_columns = {" ".join(str(key).split()) for key in (*text_plan["handled_labels"], *text_plan["handled_keys"])}
    rule_layers = {(sp, tp) for sp, tp in text_plan["used_layers"]}
    legacy_fields = [item for item in parsed
                     if not text_rules.classify_field(item.label) and item.label not in text_plan["handled_labels"]]

    def legacy_column(column: str) -> bool:
        return bool(column) and " ".join(column.split()) not in rule_columns

    main_replacements: list[dict] = list(text_plan["main"])
    main_targets: list[custom_fields.TextTarget] = []
    for layer in getattr(template, "all_layers", []) or []:
        if layer.get("kind") != "type" or not bool(layer.get("visible", True)):
            continue
        path = _path(layer.get("path_parts") or layer.get("path"))
        key = tuple(path)
        if not path or key in managed_main or ((), key) in rule_layers:
            continue
        leaf = path[-1]
        column = _text_aliases_for(text_aliases, [], path) or str(aliases.get(leaf) or "").strip() or leaf
        if legacy_column(column) and column != variant_field:
            value = values.get(column, "")
            if value:
                main_replacements.append({"path": path, "value": value})
            main_targets.append(custom_fields.TextTarget.from_parts(path, column))

    smart_tasks: list[dict] = []
    smart_configs = _identity_config(cfg, template_dir, template, "smart_object_configs", []) or []
    # Bundle and settings use different container shapes; normalize to a list.
    if isinstance(smart_configs, Mapping):
        smart_configs = list(smart_configs.values())
    smart_configs = [dict(item) for item in smart_configs]
    for profile_smart in profile_rules.get("smart_object_configs") or []:
        existing = next((item for item in smart_configs
                         if _path_key(item.get("smart_path")) == _path_key(profile_smart.get("smart_path"))), None)
        if existing is None:
            smart_configs.append(copy.deepcopy(profile_smart))
        else:
            existing["selectors"] = list(existing.get("selectors") or [])
            for selector in profile_smart.get("selectors") or []:
                _append_selector(existing["selectors"], copy.deepcopy(selector))
    smart_aliases = _identity_config(cfg, template_dir, template, "smart_object_aliases", []) or []
    custom_replacements = custom_fields.resolve_custom_info(
        "\n".join(f"{item.label}: {item.value}" for item in legacy_fields),
        main_targets,
    ).replacements if legacy_fields and main_targets else []
    custom_main = [item for item in custom_replacements if not _path(item.get("path"))[:1] == ["__smart_object_text_target_v1__"]]

    seen_object_ids: set[str] = set()
    material_holders: list[tuple[str, ...]] = []
    for layer in getattr(template, "all_layers", []) or []:
        if layer.get("kind") != "smartobject":
            continue
        info = layer.get("smart_object") or {}
        if not info.get("editable"):
            continue
        smart_path = _path(layer.get("path_parts") or layer.get("path"))
        object_id = _smart_id(layer, smart_path)
        inner = inner_layers.get(tuple(smart_path), {})
        inner_valid = set(inner)
        config_entry = next((item for item in smart_configs if _path_key(item.get("smart_path")) == tuple(smart_path)), {"selectors": []})
        smart_selectors = _as_selectors(config_entry)
        smart_selectors.extend(_infer_hide_selectors(smart_selectors, values, inner_valid))
        context = f"智能对象 {'/'.join(smart_path)} · "
        _validate_selector_paths(smart_selectors, inner_valid, errors, context=context)
        material = _material_plan(parsed, inner)
        smart_selectors = [
            item for item in smart_selectors
            if not _selector_in_scopes(item, material["managed"], inner_valid)
        ]
        visibility, selector_replacements = _selector_actions(
            smart_selectors, values, inner_valid, errors, context=context
        )
        visibility = _overlay_actions(visibility, material["actions"])
        _check_material_scopes(material["by_scope"], visibility, inner, errors, context=context)
        if material["by_scope"]:
            material_holders.append(tuple(smart_path))
        managed: set[tuple[str, ...]] = set()
        for selector in smart_selectors:
            for candidate in selector.get("candidates") or []:
                for rel in (selector.get("relative_text_aliases") or {}):
                    rel_path = _path(rel)
                    if rel_path:
                        managed.add(tuple(_path(candidate.get("path")) + rel_path))
        default: list[dict] = []
        smart_targets: list[custom_fields.TextTarget] = []
        for child in info.get("layers") or []:
            if child.get("kind") != "type" or not bool(child.get("visible", True)):
                continue
            path = _path(child.get("path_parts") or child.get("path"))
            if not path or tuple(path) in managed or (tuple(smart_path), tuple(path)) in rule_layers:
                continue
            column = (
                _text_aliases_for(text_aliases, smart_path, path)
                or _text_aliases_for(smart_aliases, smart_path, path)
                or path[-1]
            )
            if not legacy_column(column):
                continue
            if column != variant_field and values.get(column, ""):
                default.append({"path": path, "value": values[column]})
            smart_targets.append(custom_fields.TextTarget.from_parts(path, column))
        default.extend(text_plan["smart"].get(tuple(smart_path), []))
        raw_smart = custom_fields.resolve_custom_info(
            "\n".join(f"{item.label}: {item.value}" for item in legacy_fields), smart_targets
        ).replacements if legacy_fields and smart_targets else []
        task = {
            "smartObjectId": object_id,
            "smartObjectPath": smart_path,
            "visibilityActions": visibility,
            "pathReplacements": _format_text_replacements(
                _merge_path_replacements(default, selector_replacements, raw_smart)
            ),
        }
        smart_tasks.append(task)
        seen_object_ids.add(object_id)
    if not main_material["by_scope"]:
        # Material groups in the main document are covered by the group check.
        _check_material_containers(material_holders, main_visibility, main_layers, errors)

    # Merge main raw custom replacements after defaults, preserving split/multi
    # value behavior from custom_fields without allowing a first-only shortcut.
    main_replacements = _format_text_replacements(
        _merge_path_replacements(main_replacements, main_selector_replacements, custom_main)
    )
    images: list[dict] = []
    image_configs = _identity_config(cfg, template_dir, template, "image_replacement_configs", []) or []
    if isinstance(image_configs, Mapping):
        image_configs = list(image_configs.values())
    for image_config in image_configs:
        if not image_config.get("enabled"):
            continue
        column = str(image_config.get("column") or "").strip()
        value = values.get(column, "").strip()
        if not value:
            continue
        layer_path = _path(image_config.get("layer_path"))
        if tuple(layer_path) not in main_valid:
            errors.append("图片替换目标图层不存在: " + "/".join(layer_path))
            continue
        try:
            source = image_assets.resolve_mapped_image(image_config, value)
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
            continue
        layer = main_layers[tuple(layer_path)]
        action = {"path": layer_path, "sourcePath": source, "excelValue": value}
        if image_assets.is_image_replacement_layer(layer) and layer.get("kind") == "smartobject":
            action["smartObjectId"] = _smart_id(layer, layer_path)
        images.append(action)

    smart_final = _merge_smart_tasks(smart_tasks, errors)
    _attach_fit_boxes(template, [], main_replacements, text_plan["name_layers"])
    _attach_text_styles([], main_replacements, text_plan["styles"])
    for task in smart_final:
        _attach_fit_boxes(template, task["smartObjectPath"], task["pathReplacements"], text_plan["name_layers"])
        _attach_text_styles(task["smartObjectPath"], task["pathReplacements"], text_plan["styles"])
    # Image references to a shared object must also agree.  This mirrors the
    # renderer's original consolidation rule without importing main.py.
    by_image_id: dict[str, str] = {}
    image_final: list[dict] = []
    for action in images:
        object_id = str(action.get("smartObjectId") or "")
        if object_id and object_id in by_image_id and by_image_id[object_id] != action["sourcePath"]:
            errors.append("共享图片智能对象配置冲突: " + object_id)
            continue
        if object_id:
            by_image_id[object_id] = action["sourcePath"]
            if any(str(item.get("smartObjectId") or "") == object_id for item in image_final):
                continue
        image_final.append(action)

    plan = {
        "replacements": {},
        "pathReplacements": main_replacements,
        "visibilityActions": main_visibility,
        "smartObjectTextReplacements": smart_final,
        "imageReplacements": image_final,
        "errors": list(dict.fromkeys(errors)),
        "warnings": warnings,
    }
    plan["ok"] = not plan["errors"]
    plan["renderer_kwargs"] = {
        "visibility_actions": plan["visibilityActions"],
        "path_replacements": plan["pathReplacements"],
        "smart_object_text_replacements": plan["smartObjectTextReplacements"],
        "image_replacements": plan["imageReplacements"],
    }
    return plan


__all__ = [
    "ParsedField", "ParsedFields", "compile_plan", "materialize_field_values",
    "materialize_fields", "parse_fields", "profile_to_legacy_configs",
    "serialize_field_key",
]
