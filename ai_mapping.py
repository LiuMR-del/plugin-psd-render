"""Validate AI template mapping suggestions and compile render parameters."""

from __future__ import annotations

import copy
from typing import Any

import template_profiles
import text_rules

MIN_AUTO_CONFIDENCE = 0.80


def _path(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part for part in value.split("/") if part]
    return [str(part) for part in value or []]


def _field_values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value in (None, ""):
        return []
    return [str(value).strip()]


def _field_map(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key).strip(): _field_values(raw)
        for key, raw in value.items()
        if str(key).strip()
    }


def _value_key(value: Any) -> str:
    return str(value or "").strip().casefold()


def _layer_indexes(template) -> tuple[dict[tuple[str, ...], dict], dict[tuple[str, ...], dict[tuple[str, ...], dict]]]:
    """Index main-document layers and each editable smart object's inner layers."""
    main = {}
    inner: dict[tuple[str, ...], dict[tuple[str, ...], dict]] = {}
    for layer in template.all_layers:
        parts = tuple(_path(layer.get("path_parts") or layer.get("path", "").split("/")))
        if not parts:
            continue
        main[parts] = layer
        if layer.get("kind") != "smartobject":
            continue
        info = layer.get("smart_object") or {}
        if not info.get("editable"):
            continue
        inner_index = {}
        for child in info.get("layers") or []:
            child_parts = tuple(_path(child.get("path_parts") or child.get("path", "").split("/")))
            if child_parts:
                inner_index[child_parts] = child
        inner[parts] = inner_index
    return main, inner


def validate_mapping_suggestion(suggestion: dict, template, *, min_confidence: float = MIN_AUTO_CONFIDENCE) -> tuple[dict, list[str]]:
    """Sanitize AI person-style paths and name/year text mappings."""
    if not isinstance(suggestion, dict):
        return {}, ["AI 映射结果不是 JSON 对象"]
    layers, inner_layers = _layer_indexes(template)
    errors: list[str] = []
    accepted_text: list[dict] = []
    accepted_selectors: list[dict] = []
    accepted_images: list[dict] = []

    # Text suggestions are only for name/year fields the local rules could
    # not resolve; each must target an existing text layer of matching role.
    for raw in suggestion.get("text_mappings") or []:
        if not isinstance(raw, dict):
            continue
        source_key = str(raw.get("source_key") or "").strip()
        section = str(raw.get("section") or "").strip()
        target = _path(raw.get("target_path"))
        smart_path = _path(raw.get("smart_object_path"))
        target_index = inner_layers.get(tuple(smart_path), {}) if smart_path else layers
        layer = target_index.get(tuple(target))
        if not (source_key and layer and layer.get("kind") == "type"):
            errors.append(f"文字映射候选不存在: {source_key} -> " + "/".join(smart_path + target))
            continue
        item = {
            "source_key": source_key, "section": section,
            "field_key": text_rules.field_key(section, source_key),
            "kind": text_rules.classify_field(source_key),
            "target_path": target,
            **({"smart_object_path": smart_path} if smart_path else {}),
            "confidence": float(raw.get("confidence") or 0),
            "required": bool(raw.get("required")),
            "source": "ai",
        }
        role_errors = text_rules.check_mapping_roles([*accepted_text, item], template)
        if role_errors:
            errors.extend(f"文字映射未通过校验: {message}" for message in role_errors)
            continue
        accepted_text.append(item)

    person_mappings = suggestion.get("person_style_mappings")
    if person_mappings is None:
        person_mappings = suggestion.get("selector_mappings") or []
    for raw in person_mappings or []:
        if not isinstance(raw, dict):
            errors.append("人物样式映射项不是对象")
            continue
        source_key = str(raw.get("source_key") or "").strip()
        parent = _path(raw.get("parent_path"))
        smart_path = _path(raw.get("smart_object_path"))
        confidence = float(raw.get("confidence") or 0)
        target_index = inner_layers.get(tuple(smart_path), {}) if smart_path else layers
        parent_layer = target_index.get(tuple(parent))
        value_mapping: dict[str, list[list[str]]] = {}
        for value, paths in (raw.get("value_mapping") or {}).items():
            normalized_paths: list[list[str]] = []
            # An empty list is intentional for No/hide-parent values.
            for target in paths or []:
                target_path = _path(target)
                if tuple(target_path) not in target_index:
                    label = ("/".join(smart_path) + " › " if smart_path else "") + "/".join(target_path)
                    errors.append(f"人物样式候选不存在: {source_key}={value} -> {label}")
                else:
                    normalized_paths.append(target_path)
            value_key = str(value).strip()
            if normalized_paths or value_key.casefold() in {"no", "none", "null", "无", "隐藏", "hide", "hidden"}:
                value_mapping[value_key] = normalized_paths
        hide_values = [str(item).strip() for item in raw.get("hide_parent_values") or [] if str(item).strip()]
        for value in hide_values:
            value_mapping.setdefault(value, [])
        if not source_key or not parent:
            errors.append("人物样式映射缺少字段名或父级路径")
        elif parent_layer is None:
            errors.append("人物样式父级不存在: " + "/".join(smart_path + parent))
        elif not value_mapping:
            errors.append("人物样式没有有效候选: " + source_key)
        else:
            section = str(raw.get("section") or "").strip()
            accepted_selectors.append({
                "source_key": source_key,
                **({"section": section, "field_key": text_rules.field_key(section, source_key)} if section else {}),
                "parent_path": parent,
                "smart_object_path": smart_path,
                "value_mapping": value_mapping,
                "hide_parent_values": hide_values,
                "confidence": confidence,
                "required": bool(raw.get("required")),
                "auto_approved": confidence >= min_confidence,
            })

    normalized_fields = _field_map(suggestion.get("normalized_fields"))
    field_types_raw = suggestion.get("field_types")
    field_types = {
        str(key).strip(): str(value).strip()
        for key, value in (field_types_raw.items() if isinstance(field_types_raw, dict) else [])
        if str(key).strip()
    }
    for key in normalized_fields:
        field_types.setdefault(key, "unresolved")
    normalized = {
        "normalized_fields": normalized_fields,
        "field_types": field_types,
        "text_mappings": accepted_text,
        "person_style_mappings": accepted_selectors,
        "selector_mappings": accepted_selectors,
        "image_mappings": accepted_images,
        "ignored_fields": [str(item).strip() for item in suggestion.get("ignored_fields") or [] if str(item).strip()],
        "unresolved_fields": [str(item).strip() for item in suggestion.get("unresolved_fields") or [] if str(item).strip()],
    }
    return normalized, list(dict.fromkeys(errors))


def _value_for(fields: dict, key: str) -> str:
    values = fields.get(key)
    if isinstance(values, list):
        return str(values[0] if values else "").strip()
    return str(values or "").strip()


def compile_render_plan(profile: dict, template, fields: dict[str, Any], *, strict: bool = True) -> dict:
    """Compile an approved profile and one order's actual values."""
    errors: list[str] = []
    validation_errors = template_profiles.validate_profile_against_template(profile, template)
    errors.extend(validation_errors)
    if strict and str(profile.get("status") or "") != "approved":
        errors.append("模板档案尚未审核通过")
    path_replacements: list[dict] = []
    visibility_actions: list[dict] = []
    smart_tasks: dict[tuple[str, ...], dict] = {}
    for mapping in profile.get("text_mappings") or []:
        value = _value_for(fields, str(mapping.get("source_key") or ""))
        if not value:
            if mapping.get("required"):
                errors.append("缺少必填文字字段: " + str(mapping.get("source_key") or ""))
            continue
        target = _path(mapping.get("target_path"))
        smart_path = _path(mapping.get("smart_object_path"))
        if smart_path:
            task = smart_tasks.setdefault(tuple(smart_path), {
                "smartObjectId": "path:" + "/".join(smart_path),
                "smartObjectPath": smart_path,
                "visibilityActions": [],
                "pathReplacements": [],
            })
            task["pathReplacements"].append({"path": target, "value": value})
        else:
            path_replacements.append({"path": target, "value": value})
    for mapping in profile.get("selector_mappings") or profile.get("person_style_mappings") or []:
        source_key = str(mapping.get("source_key") or "")
        value = _value_for(fields, source_key)
        if not value:
            if mapping.get("required"):
                errors.append("缺少必填切换字段: " + source_key)
            continue
        value_mapping = mapping.get("value_mapping") or {}
        candidates = value_mapping.get(value)
        if candidates is None:
            folded = _value_key(value)
            candidates = next((paths for key, paths in value_mapping.items() if _value_key(key) == folded), None)
        hide_values = {_value_key(item) for item in mapping.get("hide_parent_values") or []}
        hide_parent = _value_key(value) in hide_values or candidates == []
        if candidates is None:
            errors.append(f"切换字段「{source_key}」的值「{value}」没有匹配候选")
            continue
        smart_path = _path(mapping.get("smart_object_path"))
        parent = _path(mapping.get("parent_path"))
        actions = [{"path": parent, "visible": not hide_parent}]
        all_candidates = [path for paths in value_mapping.values() for path in paths]
        for candidate in all_candidates:
            actions.append({"path": _path(candidate), "visible": False})
        if not hide_parent:
            for candidate in candidates:
                actions.append({"path": _path(candidate), "visible": True})
        if smart_path:
            task = smart_tasks.setdefault(tuple(smart_path), {
                "smartObjectId": "path:" + "/".join(smart_path),
                "smartObjectPath": smart_path,
                "visibilityActions": [],
                "pathReplacements": [],
            })
            task["visibilityActions"].extend(actions)
        else:
            visibility_actions.extend(actions)
    return {
        "replacements": {},
        "pathReplacements": path_replacements,
        "visibilityActions": visibility_actions,
        "smartObjectTextReplacements": list(smart_tasks.values()),
        "imageReplacements": [],
        "errors": list(dict.fromkeys(errors)),
        "ok": not errors,
    }
