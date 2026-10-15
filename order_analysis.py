"""Per-order template analysis: what an order still needs, the AI request for
that single order, merging the answer into the template rules, and the
deterministic check that decides whether the result may render unattended.

Agreed flow (2026-10-13):

* A template profile stores rules "field name -> layer" (never order values).
  An order whose every field is already covered just reuses the rules with
  its own values (Custom Year 1966 -> 1996, Drink_08 -> Drink_01): no AI.
* An order with fields the template has not seen (or a brand-new template)
  sends **this one order's** full customization text plus the template's
  layer summary to AI.  Orders are analyzed one by one, isolated.
* The program does not decide; it checks.  If the merged rules compile
  without errors and cover every field, they are saved as approved and the
  order renders.  Otherwise the order goes to manual review.
* Local rules are only a fallback when AI is unavailable.

Pure module: no GUI, no network, no Photoshop.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Iterable, Mapping

import local_candidates
import order_pipeline
import template_profiles
import text_rules

RULES_VERSION = 2
# Local fallback only: fields that never change the artwork.
_IGNORABLE_WORDS = {"confirm", "size", "style", "look", "option", "options", "product",
                    "type", "request", "requests", "special", "preferred"}
_NEVER_IGNORE_WORDS = {"upload", "photo", "image", "picture"}
_EMPTY_REQUEST_VALUES = {"", "no", "none", "n/a", "na", "null", "无", "没有"}


def _norm(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _ignored_labels(profile: Mapping[str, Any] | None) -> set[str]:
    result: set[str] = set()
    for raw in (profile or {}).get("ignored_fields") or []:
        text = str(raw or "")
        if text.startswith("["):
            try:
                text = str(json.loads(text)[1])
            except (ValueError, IndexError, TypeError):
                pass
        result.add(_norm(text))
    return result


def is_rules_profile(profile: Mapping[str, Any] | None) -> bool:
    """Profiles from the per-order flow; older approved profiles keep their
    previous behaviour (approved = ready) and never trigger AI again."""
    return bool(profile) and int((profile or {}).get("rules_version") or 0) >= RULES_VERSION


def _fields(parsed: Iterable[Any]) -> list[dict]:
    result = []
    for item in parsed:
        result.append({"section": str(item.section).strip(), "label": str(item.label).strip(),
                       "value": str(item.value).strip()})
    return result


def field_coverage(template, parsed: Iterable[Any], profile: Mapping[str, Any] | None) -> tuple[list[dict], list[dict]]:
    """Split one order's fields into (covered, uncovered) by the profile rules."""
    parsed = list(parsed)
    material = order_pipeline.material_field_states(template, parsed)
    ignored = _ignored_labels(profile)
    text_labels = {_norm(item.get("source_key")) for item in (profile or {}).get("text_mappings") or []}
    selector_values: dict[str, set[str]] = {}
    for mapping in (profile or {}).get("selector_mappings") or []:
        label = _norm(mapping.get("source_key"))
        values = {_norm(value) for value in (mapping.get("value_mapping") or {})}
        values |= {_norm(value) for value in mapping.get("hide_parent_values") or []}
        selector_values.setdefault(label, set()).update(values)
    styles = {(item["section"], _norm(item["label"])): item
              for item in text_rules.style_fields(parsed, profile)}
    covered: list[dict] = []
    uncovered: list[dict] = []
    for field in _fields(parsed):
        label = _norm(field["label"])
        kind = text_rules.classify_field(field["label"])
        style = styles.get((field["section"], label))
        material_state = material.get((field["section"], label))
        how = ""
        if label in ignored:
            how = "ignored"
        elif kind:
            how = "text" if label in text_labels else ""
        elif style:
            # Font names and #hex colours are used as written; a colour name
            # needs its hex value from the profile (AI analysis / review).
            how = "style" if text_rules.style_value(style, profile) else ""
            kind = style["style"]
        elif label in text_labels:
            how, kind = "text", text_rules.TEXT
        elif material_state:
            how = "material"
        elif _norm(field["value"]) in selector_values.get(label, set()):
            how = "selector"
        entry = {**field, "kind": kind or ("material" if material_state is not None else "other"), "how": how}
        (covered if how else uncovered).append(entry)
    return covered, uncovered


def uncovered_signature(uncovered: Iterable[Mapping[str, Any]]) -> str:
    """Stable id of 'what this order still needs' (used to avoid re-asking)."""
    payload = sorted((_norm(item.get("section")), _norm(item.get("label")), _norm(item.get("value")))
                     for item in uncovered)
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _layer_outline(template, limit: int = 250) -> list[dict]:
    """Compact layer summary: groups/layers up to depth 2 and smart-object roots."""
    outline: list[dict] = []
    for layer in getattr(template, "all_layers", []) or []:
        if int(layer.get("depth") or 0) > 2:
            continue
        item = {"path": list(layer.get("path_parts") or []), "kind": layer.get("kind"),
                "visible": bool(layer.get("visible", True))}
        info = layer.get("smart_object") or {}
        if info:
            item["smart_object"] = {"editable": bool(info.get("editable")), "file": info.get("filename", "")}
        outline.append(item)
        if len(outline) >= limit:
            break
    return outline


def ai_request(template, parsed: Iterable[Any], covered: list[dict], uncovered: list[dict]) -> dict:
    """Payload for one isolated order: full text, what is open, layer summary."""
    parsed = list(parsed)
    material_fields = [
        {"section_identity": item["section"], "source_key": item["label"], "value": item["value"]}
        for item in uncovered if item["kind"] == "material"
    ]
    package = local_candidates.build_candidate_package(template, material_fields) if material_fields else {}
    return {
        "customization_text": _fields(parsed),
        "fields_to_map": [{key: item[key] for key in ("section", "label", "value", "kind")} for item in uncovered],
        "already_handled": [{key: item[key] for key in ("section", "label", "how")} for item in covered],
        "text_layers": [
            {key: layer[key] for key in ("smart_object_path", "target_path", "text", "bbox", "role")}
            for layer in text_rules.text_layers(template)
        ],
        "material_candidates": package.get("candidates") or [],
        "layer_outline": _layer_outline(template),
        "canvas": [getattr(template, "width", 0), getattr(template, "height", 0)],
    }


def local_suggestion(template, uncovered: list[dict]) -> dict:
    """Fallback when AI is unavailable: deterministic text rules + obvious ignores."""
    text_items = [item for item in uncovered if text_rules.classify_field(item["label"])]
    mappings, _ = text_rules.local_mappings(template, text_items)
    ignored = []
    for item in uncovered:
        tokens = set(text_rules._tokens(item["label"]))  # noqa: SLF001 - same tokenizer on purpose
        if item["kind"] == "other" and tokens & _IGNORABLE_WORDS and not tokens & _NEVER_IGNORE_WORDS:
            ignored.append(item["label"])
    return {"text_mappings": mappings, "person_style_mappings": [], "selector_mappings": [],
            "style_mappings": [], "color_values": {},
            "ignored_fields": ignored, "unresolved_fields": []}


def merge_rules(template, profile: Mapping[str, Any] | None, suggestion: Mapping[str, Any],
                open_fields: Iterable[Mapping[str, Any]] = ()) -> dict:
    """Existing rules + new rules for this order's open fields (existing wins).

    Name/year fields the suggestion left unmapped (or whose AI mapping was
    rejected) are filled from the deterministic text rules when those are
    certain, so the saved rules are complete and later orders need no AI.
    """
    if profile:
        merged = copy.deepcopy(dict(profile))
    else:
        merged = template_profiles.make_profile(
            sku=template.sku, size_key=template.size_key, template_filename=template.filename,
            structure_signature=template.structure_signature, status="draft",
        )
    text = list(merged.get("text_mappings") or [])
    known = {_norm(item.get("source_key")) for item in text}
    for item in suggestion.get("text_mappings") or []:
        if _norm(item.get("source_key")) not in known:
            text.append(dict(item))
            known.add(_norm(item.get("source_key")))
    selectors = list(merged.get("selector_mappings") or [])
    for item in suggestion.get("person_style_mappings") or suggestion.get("selector_mappings") or []:
        same = next((existing for existing in selectors
                     if _norm(existing.get("source_key")) == _norm(item.get("source_key"))
                     and list(existing.get("parent_path") or []) == list(item.get("parent_path") or [])
                     and list(existing.get("smart_object_path") or []) == list(item.get("smart_object_path") or [])),
                    None)
        if same is None:
            selectors.append(copy.deepcopy(dict(item)))
        else:
            for value, paths in (item.get("value_mapping") or {}).items():
                same.setdefault("value_mapping", {}).setdefault(value, paths)
            same["hide_parent_values"] = list(dict.fromkeys([
                *(same.get("hide_parent_values") or []), *(item.get("hide_parent_values") or []),
            ]))
    styles = list(merged.get("style_mappings") or [])
    style_labels = {_norm(item.get("source_key")) for item in styles}
    for item in suggestion.get("style_mappings") or []:
        if _norm(item.get("source_key")) not in style_labels | known:
            styles.append(copy.deepcopy(dict(item)))
            style_labels.add(_norm(item.get("source_key")))
    colors = dict(suggestion.get("color_values") or {})
    colors.update(merged.get("color_values") or {})
    ignored_now = _ignored_labels({"ignored_fields": [*(merged.get("ignored_fields") or []),
                                                     *(suggestion.get("ignored_fields") or [])]})
    gaps = [item for item in open_fields
            if text_rules.classify_field(item.get("label", "")) and _norm(item.get("label")) not in known
            and _norm(item.get("label")) not in ignored_now]
    if gaps:
        local, _ = text_rules.local_mappings(template, gaps)
        for item in local:
            if not text_rules.check_mapping_roles([*text, item], template):
                text.append(item)
                known.add(_norm(item.get("source_key")))
    merged["text_mappings"] = text
    merged["selector_mappings"] = selectors
    merged["style_mappings"] = styles
    merged["color_values"] = colors
    merged["ignored_fields"] = list(dict.fromkeys([
        *(merged.get("ignored_fields") or []), *(suggestion.get("ignored_fields") or []),
    ]))
    merged["rules_version"] = RULES_VERSION
    merged["text_reviewed"] = True
    return merged


def check_candidate(template, row, cfg: Mapping[str, Any], template_dir: str,
                    candidate: Mapping[str, Any]) -> list[str]:
    """Errors that block unattended use of ``candidate`` for this order."""
    parsed = order_pipeline.parse_fields(getattr(row, "custom_info", ""))
    plan = order_pipeline.compile_plan(template, copy.deepcopy(row), {**dict(cfg), "profile": candidate},
                                       template_dir, strict=False)
    errors = list(plan.get("errors") or [])
    errors.extend(text_rules.check_mapping_roles(candidate.get("text_mappings") or [], template))
    _, uncovered = field_coverage(template, parsed, candidate)
    # Text fields already report precise errors from the text check above.
    errors.extend(f"字段「{item['label']}」({item['value']}) 没有对应的图层规则"
                  for item in uncovered
                  if not text_rules.classify_field(item["label"]) and item["kind"] not in text_rules.STYLE_LABELS)
    return list(dict.fromkeys(errors))


def special_request_warnings(parsed: Iterable[Any]) -> list[str]:
    """Free-text customer requests cannot be rendered automatically; surface them."""
    warnings = []
    for item in parsed:
        tokens = set(text_rules._tokens(item.label))  # noqa: SLF001
        if tokens & {"request", "requests"} and _norm(item.value) not in _EMPTY_REQUEST_VALUES:
            warnings.append(f"客户特殊要求「{item.value}」需人工确认")
    return warnings


__all__ = [
    "RULES_VERSION", "ai_request", "check_candidate", "field_coverage", "is_rules_profile",
    "local_suggestion", "merge_rules", "special_request_warnings", "uncovered_signature",
]
