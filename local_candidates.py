"""Local person-material candidate indexing and deterministic matching.

This module deliberately never calls AI or Photoshop. It reduces a scanned PSD
layer tree to the small set of person-material groups relevant to the current
order fields; the remaining ambiguous decisions may be sent to AI.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict


def _field_key(section: str, source_key: str) -> str:
    return json.dumps([str(section or "").strip(), str(source_key or "").strip()], ensure_ascii=False, separators=(",", ":"))
from typing import Any, Iterable, Mapping


HIDE_VALUES = {"no", "none", "null", "无", "隐藏", "hide", "hidden"}
TEXT_HINTS = {
    "name", "year", "date", "quote", "message", "text", "style", "size",
    "confirm", "font", "color", "request", "special", "information",
}
PERSON_HINTS = {
    "hair", "glasses", "beard", "drink", "pants", "top", "shirt", "hoodie",
    "sweater", "chair", "age", "skin", "tone", "look", "character",
}
CATEGORY_HINTS = {
    "hair": ("hair",),
    "glasses": ("glasses", "spectacles"),
    "beard": ("beard",),
    "drink": ("drink", "beverage", "cup"),
    "pants": ("pants", "trousers"),
    "top": ("top", "shirt", "hoodie", "sweater"),
    "chair": ("chair",),
    "age": ("age", "skin", "tone"),
}


def _path(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part for part in value.replace("\\", "/").split("/") if part]
    return [str(part) for part in value or [] if str(part)]


def _tokens(value: Any) -> set[str]:
    text = str(value or "").casefold().replace("’", "'")
    return set(re.findall(r"[a-z0-9]+", text))


def classify_field(source_key: str) -> str:
    tokens = _tokens(source_key)
    if tokens & PERSON_HINTS and not ({"look", "character"} & tokens):
        return "person_style"
    if tokens & TEXT_HINTS:
        return "text"
    return "text"


def _role_tokens(source_key: str, section: str) -> set[str]:
    tokens = _tokens(f"{section} {source_key}")
    return {role for role in ("man", "woman", "male", "female") if role in tokens}


def _section_side(section: str) -> str:
    """Return 'man'/'woman' by token, avoiding the 'man' in 'woman' trap."""
    tokens = _tokens(section)
    if "woman" in tokens or "female" in tokens:
        return "woman"
    if "man" in tokens or "male" in tokens:
        return "man"
    return ""


def _category_tokens(source_key: str) -> set[str]:
    tokens = _tokens(source_key)
    categories: set[str] = set()
    for category, hints in CATEGORY_HINTS.items():
        if any(hint in tokens for hint in hints):
            categories.add(category)
    return categories


def _norm_value(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _value_from_path(path: list[str]) -> str:
    if not path:
        return ""
    leaf = path[-1].strip()
    # Technical layer names commonly prefix the business value.
    if "_" in leaf:
        return leaf.split("_", 1)[1].strip()
    return leaf


def _is_person_path(path: list[str]) -> bool:
    tokens = _tokens("/".join(path))
    return bool(tokens & PERSON_HINTS) and bool(tokens & {"man", "woman", "male", "female"})


def _semantic_categories(value: Any) -> set[str]:
    tokens = _tokens(value)
    result: set[str] = set()
    for category, hints in CATEGORY_HINTS.items():
        if any(hint in tokens for hint in hints):
            result.add(category)
    return result


def _has_semantic_ancestor(path: tuple[str, ...], valid: Mapping[tuple[str, ...], dict]) -> bool:
    """Skip nested candidate branches when their ancestor is the business group."""
    own_categories = _semantic_categories(path[-1] if path else "")
    if not own_categories:
        return False
    for index in range(1, len(path)):
        ancestor = path[:index]
        layer = valid.get(ancestor) or {}
        if layer.get("kind") == "group" and _semantic_categories(ancestor[-1]) & own_categories:
            return True
    return False


def _candidate_aliases(path: list[str], value: str) -> list[str]:
    leaf = path[-1] if path else value
    aliases = [str(value or ""), leaf]
    if "_" in leaf:
        aliases.append(leaf.split("_", 1)[1])
    if len(path) > 1:
        aliases.append(path[-2])
    return list(dict.fromkeys(alias for alias in aliases if alias.strip()))


def _contexts(template) -> list[tuple[list[str], list[dict]]]:
    contexts: list[tuple[list[str], list[dict]]] = [( [], list(getattr(template, "all_layers", []) or []) )]
    for layer in getattr(template, "all_layers", []) or []:
        if layer.get("kind") != "smartobject":
            continue
        info = layer.get("smart_object") or {}
        if info.get("editable"):
            contexts.append((_path(layer.get("path_parts")), list(info.get("layers") or [])))
    return contexts


def _candidate_groups(template) -> list[dict]:
    """Return compact person-material groups from main and smart-object layers."""
    result: list[dict] = []
    for smart_path, layers in _contexts(template):
        valid = {tuple(_path(layer.get("path_parts"))): layer for layer in layers if _path(layer.get("path_parts"))}
        for parent_path, parent in valid.items():
            if parent.get("kind") != "group" or not _is_person_path(list(parent_path)):
                continue
            if _has_semantic_ancestor(parent_path, valid):
                continue
            descendants = [
                (path, layer) for path, layer in valid.items()
                if len(path) > len(parent_path) and path[:len(parent_path)] == parent_path
            ]
            if not descendants:
                continue
            direct = [
                (path, layer) for path, layer in descendants
                if len(path) == len(parent_path) + 1
            ]
            # Prefer direct child branches. If the group directly contains
            # technical leaves, those are the candidates; otherwise retain the
            # branch plus its leaves in a compact form.
            candidate_rows: list[dict] = []
            for child_path, child in sorted(direct):
                child_desc = [
                    (path, layer) for path, layer in descendants
                    if len(path) > len(child_path) and path[:len(child_path)] == child_path
                ]
                leaves = [path for path, layer in child_desc if layer.get("kind") != "group"]
                if child.get("kind") != "group" or not leaves:
                    leaves = [child_path]
                for leaf_path in leaves:
                    candidate_rows.append({
                        "value": _value_from_path(list(leaf_path)),
                        "aliases": _candidate_aliases(list(leaf_path), _value_from_path(list(leaf_path))),
                        "path": list(leaf_path),
                        "visible": bool(valid[leaf_path].get("visible", True)),
                    })
            if not candidate_rows:
                continue
            result.append({
                "smart_object_path": list(smart_path),
                "parent_path": list(parent_path),
                "name": parent_path[-1],
                "candidates": candidate_rows,
            })
    # Embedded person artwork is often duplicated as copied smart objects.
    # Keep one canonical business group rather than sending every copy to AI.
    deduped: dict[tuple[tuple[str, ...], tuple[tuple[str, ...], ...]], dict] = {}
    for group in result:
        key = (
            tuple(group.get("parent_path") or []),
            tuple(tuple(candidate.get("path") or []) for candidate in group.get("candidates") or []),
        )
        current = deduped.get(key)
        if current is None or len(group.get("smart_object_path") or []) < len(current.get("smart_object_path") or []):
            deduped[key] = group
    return list(deduped.values())

def _matching_groups(source_key: str, section: str, values: Iterable[str], groups: Iterable[dict]) -> list[dict]:
    roles = _role_tokens(source_key, section)
    categories = _category_tokens(source_key)
    section_side = _section_side(section)
    matches: list[tuple[int, dict]] = []
    for group in groups:
        tokens = _tokens("/".join(group.get("parent_path") or []) + " " + str(group.get("name") or ""))
        score = 0
        if roles and roles & tokens:
            score += 4
        # NOTE: plain substring checks confuse 'Woman素材' with 'Man素材'
        # ('man素材' in 'Woman素材' is True). Compare per-segment tokens.
        if section_side == "man" and any(
            "man" in _tokens(part) and "woman" not in _tokens(part)
            for part in (group.get("parent_path") or [])
        ):
            score += 4
        if section_side == "woman" and any(
            "woman" in _tokens(part) for part in (group.get("parent_path") or [])
        ):
            score += 4
        if categories and any(any(hint in tokens for hint in CATEGORY_HINTS[category]) for category in categories):
            score += 5
        normalized_values = {_norm_value(value) for value in values if str(value).strip()}
        candidate_values = {
            _norm_value(alias)
            for candidate in group.get("candidates") or []
            for alias in (candidate.get("aliases") or [candidate.get("value")])
        }
        if normalized_values & candidate_values:
            score += 8
        if score:
            matches.append((score, group))
    best = max((score for score, _ in matches), default=0)
    return [group for score, group in matches if score == best] if best else []


def build_candidate_package(template, fields: Iterable[Mapping[str, Any]]) -> dict:
    """Build the bounded AI package; never include the full PSD tree."""
    groups = _candidate_groups(template)
    person_fields: list[dict] = []
    candidate_sections: list[dict] = []
    for field in fields:
        source_key = str(field.get("source_key") or field.get("label") or "").strip()
        section = str(field.get("section_identity") or field.get("section") or "").strip()
        values = field.get("values")
        if values is None:
            values = [field.get("value", "")]
        values = [str(value).strip() for value in values or [] if str(value).strip()]
        if not source_key or classify_field(source_key) != "person_style":
            continue
        person_fields.append({"section": section, "source_key": source_key, "values": values})
        matches = _matching_groups(source_key, section, values, groups)
        candidate_sections.append({
            "section": section,
            "source_key": source_key,
            "values": values,
            "candidates": matches,
        })
    # Only ambiguous fields are sent to AI. Locally resolved fields are kept
    # in a compact summary for audit/constraint context.
    local_mappings, ambiguities = deterministic_mappings({"candidates": candidate_sections})
    ambiguous_sections = [item for item in candidate_sections if any(
        item.get("section") == amb.get("section") and item.get("source_key") == amb.get("source_key")
        for amb in ambiguities
    )]
    compact_sections: list[dict] = []
    for item in ambiguous_sections:
        requested = {_norm_value(value) for value in item.get("values") or []}
        compact_groups: list[dict] = []
        for group in item.get("candidates") or []:
            candidates = list(group.get("candidates") or [])
            matching = [candidate for candidate in candidates if requested & {
                _norm_value(alias)
                for alias in (candidate.get("aliases") or [candidate.get("value")])
            }]
            selected = matching + [candidate for candidate in candidates if candidate not in matching]
            selected = selected[:4]
            compact_groups.append({
                "smart_object_path": group.get("smart_object_path") or [],
                "parent_path": group.get("parent_path") or [],
                "name": group.get("name") or "",
                "candidates": [
                    {"value": candidate.get("value", ""), "path": candidate.get("path") or []}
                    for candidate in selected
                ],
            })
        compact_sections.append({
            "section": item.get("section", ""),
            "source_key": item.get("source_key", ""),
            "values": item.get("values") or [],
            "candidates": compact_groups[:4],
        })
    package = {
        "fields": [item for item in person_fields if any(
            item.get("section") == section.get("section") and item.get("source_key") == section.get("source_key")
            for section in compact_sections
        )],
        "candidates": compact_sections,
        "local_mappings": local_mappings,
        "group_count": len(groups),
        "candidate_count": sum(len(item.get("candidates") or []) for item in ambiguous_sections),
    }
    # Guard against accidentally reintroducing a full-tree-sized request.
    if len(json.dumps(package, ensure_ascii=False).encode("utf-8")) > 64 * 1024:
        for item in package["candidates"]:
            item["candidates"] = item.get("candidates", [])[:3]
    return package


def deterministic_mappings(candidate_package: Mapping[str, Any]) -> tuple[list[dict], list[dict]]:
    """Resolve only exact/unique values locally; return mappings and ambiguities."""
    mappings: list[dict] = [
        dict(item) for item in (candidate_package.get("local_mappings") or [])
        if isinstance(item, Mapping)
    ]
    existing_keys = {
        (str(item.get("section") or ""), str(item.get("source_key") or ""))
        for item in mappings
    }
    ambiguities: list[dict] = []
    for item in candidate_package.get("candidates") or []:
        source_key = str(item.get("source_key") or "")
        section = str(item.get("section") or "")
        values = [str(value).strip() for value in item.get("values") or [] if str(value).strip()]
        groups = item.get("candidates") or []
        if len(groups) != 1:
            # A field with several identical parent groups is safe to resolve
            # when all groups expose the same normalized candidate value/path.
            signatures = {
                (
                    tuple(group.get("parent_path") or []),
                    tuple(sorted(_norm_value(candidate.get("value")) for candidate in group.get("candidates") or [])),
                )
                for group in groups
            }
            if len(signatures) == 1:
                groups = [groups[0]]
            else:
                ambiguities.append({"section": section, "source_key": source_key, "values": values, "candidates": groups})
                continue
        group = groups[0]
        by_value = defaultdict(list)
        for candidate in group.get("candidates") or []:
            for alias in candidate.get("aliases") or [candidate.get("value")]:
                by_value[_norm_value(alias)].append(candidate.get("path"))
        value_mapping = {}
        unresolved = False
        for value in values:
            found = by_value.get(value.casefold(), [])
            if not found:
                normalized = _norm_value(value)
                found = [path for key, paths in by_value.items() if _norm_value(key) == normalized for path in paths]
            if value.casefold() in HIDE_VALUES:
                value_mapping[value] = []
            elif len(found) == 1:
                value_mapping[value] = [found[0]]
            else:
                unresolved = True
        if unresolved:
            ambiguities.append({"section": section, "source_key": source_key, "values": values, "candidates": [group]})
        else:
            if (section, source_key) in existing_keys:
                continue
            mappings.append({
                "section": section,
                "field_key": _field_key(section, source_key),
                "source_key": source_key,
                "parent_path": group.get("parent_path") or [],
                "smart_object_path": group.get("smart_object_path") or [],
                "value_mapping": value_mapping,
                "hide_parent_values": [value for value in values if value.casefold() in HIDE_VALUES],
                "confidence": 1.0,
                "auto_approved": True,
                "source": "local",
            })
    return mappings, ambiguities


__all__ = ["HIDE_VALUES", "build_candidate_package", "classify_field", "deterministic_mappings"]
