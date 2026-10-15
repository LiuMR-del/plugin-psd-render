"""模板档案的持久化、校验与结构变更状态。

档案只保存可复用的映射规则，不保存具体订单中的姓名、年份等动态值。
档案文件跟随模板库，避免把业务模板配置混入用户级 UI 配置。
"""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timezone
from typing import Any

PROFILE_FILENAME = ".psd-render-profiles.json"
SCHEMA_VERSION = 1
VALID_STATUSES = {"draft", "needs_review", "approved", "blocked"}


def profile_path(template_dir: str) -> str:
    return os.path.join(os.path.abspath(template_dir), PROFILE_FILENAME)


def _empty_store() -> dict:
    return {"schema_version": SCHEMA_VERSION, "profiles": {}}


def _read_store_file(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
        return None
    return data


def load_store(template_dir: str) -> dict:
    data = _read_store_file(profile_path(template_dir))
    if data is None:
        return _empty_store()
    result = _empty_store()
    result["profiles"] = data["profiles"]
    if isinstance(data.get("merged_sources"), dict):
        result["merged_sources"] = data["merged_sources"]
    return result


def save_store(template_dir: str, store: dict) -> str:
    path = profile_path(template_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "profiles": store.get("profiles") if isinstance(store, dict) else {},
    }
    if isinstance(store, dict) and store.get("merged_sources"):
        payload["merged_sources"] = store["merged_sources"]
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)
    return path


def merge_nested_stores(template_dir: str) -> int:
    """Import profiles of sub-folders once used as libraries on their own.

    A template master library holds one folder per category; profiles saved
    while a category folder was the library live in that folder. Profiles the
    library store lacks are copied in (the library's own entry wins); the
    source files are left untouched. ``merged_sources`` records each source's
    mtime so a profile deleted here is not imported again unless that source
    changes. Returns how many profiles were imported.
    """
    root = os.path.abspath(template_dir)
    store = load_store(root)
    sources = dict(store.get("merged_sources") or {})
    imported = 0
    changed = False
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        if current == root or PROFILE_FILENAME not in files:
            continue
        path = os.path.join(current, PROFILE_FILENAME)
        relative = os.path.relpath(path, root).replace(os.sep, "/")
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        if sources.get(relative) == mtime:
            continue
        data = _read_store_file(path)
        if data is None:
            continue
        for key, profile in data["profiles"].items():
            if isinstance(profile, dict) and key not in store["profiles"]:
                store["profiles"][key] = copy.deepcopy(profile)
                imported += 1
        sources[relative] = mtime
        changed = True
    if changed:
        store["merged_sources"] = sources
        save_store(root, store)
    return imported


def profile_key(sku: str, size_key: str, filename: str, template_path: str = "") -> str:
    """Profile identity includes PSD path when available, avoiding nested collisions."""
    parts = (str(sku or "").strip(), str(size_key or "").strip(), str(filename or "").strip())
    path = os.path.abspath(str(template_path or "")) if template_path else ""
    return "|".join((*parts, path)) if path else "|".join(parts)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def make_profile(*, sku: str, size_key: str, template_filename: str,
                 structure_signature: str, text_mappings: list[dict] | None = None,
                 selector_mappings: list[dict] | None = None,
                 image_mappings: list[dict] | None = None,
                 ignored_fields: list[str] | None = None,
                 status: str = "draft", review: dict | None = None,
                 binding: dict | None = None, analysis: dict | None = None,
                 normalized_fields: dict | None = None, field_types: dict | None = None,
                 version: int = 1) -> dict:
    if status not in VALID_STATUSES:
        raise ValueError(f"无效模板档案状态: {status}")
    return {
        "schema_version": SCHEMA_VERSION,
        "sku": str(sku or "").strip(),
        "size_key": str(size_key or "").strip(),
        "template_filename": str(template_filename or "").strip(),
        "status": status,
        "version": max(1, int(version or 1)),
        "structure_signature": str(structure_signature or ""),
        "text_mappings": copy.deepcopy(text_mappings or []),
        "selector_mappings": copy.deepcopy(selector_mappings or []),
        "image_mappings": copy.deepcopy(image_mappings or []),
        "ignored_fields": [str(value).strip() for value in (ignored_fields or []) if str(value).strip()],
        "review": copy.deepcopy(review or {}),
        "template_binding": copy.deepcopy(binding or {}),
        "analysis": copy.deepcopy(analysis or {}),
        "normalized_fields": copy.deepcopy(normalized_fields or {}),
        "field_types": copy.deepcopy(field_types or {}),
    }


def _bound_file_exists(profile: dict) -> bool:
    path = str((profile.get("template_binding") or {}).get("absolute_path") or "")
    return bool(path) and os.path.isfile(path)


def matched_profile_key(store: dict, sku: str, size_key: str, filename: str, template_path: str = "") -> str | None:
    """Key of the profile a template resolves to (see ``get_profile``), or None."""
    profiles = store.get("profiles") or {}
    key = profile_key(sku, size_key, filename, template_path)
    if isinstance(profiles.get(key), dict):
        return key
    # Migration fallback for profiles saved before path-based identity existed.
    legacy = profile_key(sku, size_key, filename)
    if isinstance(profiles.get(legacy), dict):
        return legacy
    # The library may be copied/moved to another machine or folder, so the
    # absolute path in the key no longer matches. Fall back to SKU + size +
    # filename when that is unique; the structure signature still guards
    # against a different PSD with the same name. A profile whose bound PSD
    # still exists belongs to that file, never to a same-named copy elsewhere.
    matches = [
        candidate for candidate, value in profiles.items()
        if isinstance(value, dict)
        and str(value.get("sku") or "") == str(sku or "")
        and str(value.get("size_key") or "") == str(size_key or "")
        and str(value.get("template_filename") or "") == str(filename or "")
        and not _bound_file_exists(value)
    ]
    return matches[0] if len(matches) == 1 else None


def get_profile(store: dict, sku: str, size_key: str, filename: str, template_path: str = "") -> dict | None:
    key = matched_profile_key(store, sku, size_key, filename, template_path)
    return copy.deepcopy((store.get("profiles") or {})[key]) if key else None


def put_profile(store: dict, profile: dict) -> str:
    if not isinstance(profile, dict):
        raise TypeError("模板档案必须是对象")
    status = str(profile.get("status") or "draft")
    if status not in VALID_STATUSES:
        raise ValueError(f"无效模板档案状态: {status}")
    sku = str(profile.get("sku") or "").strip()
    size_key = str(profile.get("size_key") or "").strip()
    filename = str(profile.get("template_filename") or "").strip()
    # 尺寸可以为空: 相框等文件名不带尺寸的模板（2026-10-15）
    if not sku or not filename:
        raise ValueError("模板档案缺少 SKU 或文件名")
    normalized = copy.deepcopy(profile)
    normalized["schema_version"] = SCHEMA_VERSION
    normalized["sku"] = sku
    normalized["size_key"] = size_key
    normalized["template_filename"] = filename
    normalized.setdefault("version", 1)
    normalized.setdefault("review", {})
    normalized.setdefault("template_binding", {})
    normalized.setdefault("analysis", {})
    template_path = str((normalized.get("template_binding") or {}).get("absolute_path") or "")
    key = profile_key(sku, size_key, filename, template_path)
    store.setdefault("profiles", {})[key] = normalized
    return key


def bind_template(template, template_dir: str) -> dict:
    """Create the immutable identity used when an approved profile becomes built-in."""
    absolute_path = os.path.abspath(str(template.path or ""))
    try:
        relative_path = os.path.relpath(absolute_path, os.path.abspath(template_dir)).replace(os.sep, "/")
    except ValueError:
        relative_path = str(template.filename or "")
    return {
        "absolute_path": absolute_path,
        "relative_path": relative_path,
        "sku": str(template.sku or ""),
        "size_key": str(template.size_key or ""),
        "filename": str(template.filename or ""),
        "structure_signature": str(template.structure_signature or ""),
    }


def make_analysis(*, state: str, model: str = "", order_field_samples: dict | None = None,
                  unresolved_fields: list[str] | None = None, message: str = "",
                  blocking_errors: list[str] | None = None, started_at: str = "",
                  finished_at: str = "", deadline_at: str = "", task_id: str = "",
                  phase: str = "", attempt: int = 0, duration_ms: int = 0,
                  error_kind: str = "") -> dict:
    return {
        "state": str(state or ""),
        "model": str(model or ""),
        "created_at": _now(),
        "started_at": str(started_at or ""),
        "finished_at": str(finished_at or ""),
        "deadline_at": str(deadline_at or ""),
        "task_id": str(task_id or ""),
        "phase": str(phase or ""),
        "attempt": max(0, int(attempt or 0)),
        "duration_ms": max(0, int(duration_ms or 0)),
        "error_kind": str(error_kind or ""),
        "order_field_samples": copy.deepcopy(order_field_samples or {}),
        "unresolved_fields": [str(item) for item in unresolved_fields or [] if str(item).strip()],
        "message": str(message or ""),
        "blocking_errors": [str(item) for item in blocking_errors or [] if str(item).strip()],
    }


def delete_profile(store: dict, sku: str, size_key: str, filename: str, template_path: str = "") -> bool:
    """Remove only a binding/profile; never touch the PSD source file."""
    key = matched_profile_key(store, sku, size_key, filename, template_path)
    if key is None:
        return False
    del store["profiles"][key]
    return True


def approve_profile(profile: dict, structure_signature: str, model: str = "") -> dict:
    """Approve only against the current scan signature."""
    result = copy.deepcopy(profile)
    result["status"] = "approved"
    result["structure_signature"] = str(structure_signature or "")
    result["review"] = {
        **dict(result.get("review") or {}),
        "state": "approved",
        "approved_at": _now(),
        "model": str(model or ""),
    }
    return result


def _parse_iso_timestamp(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def profile_status(profile: dict | None, current_signature: str, *, stale_after_seconds: int = 900) -> tuple[str, str]:
    if not profile:
        return "needs_review", "没有已保存的模板档案"
    status = str(profile.get("status") or "draft")
    if status not in VALID_STATUSES:
        return "blocked", "模板档案状态无效"
    expected = str(profile.get("structure_signature") or "")
    current = str(current_signature or "")
    if status == "approved" and expected and expected != current:
        return "needs_review", "PSD 图层结构已变化，需要重新审核"
    if status == "approved" and not expected:
        return "needs_review", "模板档案缺少结构签名"
    if status == "draft":
        analysis = dict(profile.get("analysis") or {})
        analysis_state = str(analysis.get("state") or "")
        if analysis_state == "analyzing":
            analysis_started = _parse_iso_timestamp(analysis.get("started_at") or analysis.get("created_at"))
            deadline = _parse_iso_timestamp(analysis.get("deadline_at"))
            now = datetime.now(timezone.utc)
            stale = (
                deadline is not None and deadline < now
            ) or (
                deadline is None and analysis_started is not None
                and (now - analysis_started).total_seconds() > max(1, int(stale_after_seconds))
            )
            if stale:
                return "needs_review", "AI 分析任务已超时，可重新分析"
            return "needs_review", "AI 正在分析定制字段和图层"
        if analysis_state == "ready":
            return "needs_review", "AI 草稿已生成，等待人工审核"
        if analysis_state == "failed":
            return "needs_review", "AI 分析失败，可重新分析"
        return "needs_review", "模板档案尚未审核"
    return status, str((profile.get("review") or {}).get("message") or "")


def _path_set(layers: list[dict]) -> dict[tuple[str, ...], str]:
    return {
        tuple(str(part) for part in layer.get("path_parts") or []): str(layer.get("kind") or "")
        for layer in layers
        if layer.get("path_parts")
    }


def validate_profile_against_template(profile: dict, template) -> list[str]:
    """Validate saved targets against a scanned Template without changing it."""
    errors: list[str] = []
    if str(profile.get("sku") or "") != str(template.sku):
        errors.append("模板档案 SKU 与当前模板不一致")
    if str(profile.get("size_key") or "") != str(template.size_key):
        errors.append("模板档案尺寸与当前模板不一致")
    if str(profile.get("template_filename") or "") != str(template.filename):
        errors.append("模板档案文件名与当前模板不一致")
    if str(profile.get("structure_signature") or "") and profile["structure_signature"] != template.structure_signature:
        errors.append("PSD 图层结构签名已变化")

    paths = _path_set(template.all_layers)
    def check_path(raw_path: Any, expected_kind: str = "") -> None:
        path = tuple(str(part) for part in raw_path or [])
        if not path:
            errors.append("存在空的图层路径")
            return
        actual = paths.get(path)
        if actual is None:
            errors.append("找不到图层路径: " + "/".join(path))
        elif expected_kind and actual != expected_kind:
            errors.append(f"图层类型不匹配: {'/'.join(path)}（需要 {expected_kind}，实际 {actual}）")

    inner_by_smart: dict[tuple[str, ...], dict[tuple[str, ...], str]] = {}
    for layer in template.all_layers:
        if layer.get("kind") != "smartobject":
            continue
        smart_path = tuple(str(part) for part in layer.get("path_parts") or [])
        info = layer.get("smart_object") or {}
        if not info.get("editable"):
            continue
        inner_by_smart[smart_path] = {
            tuple(str(part) for part in child.get("path_parts") or []): str(child.get("kind") or "")
            for child in info.get("layers") or []
            if child.get("path_parts")
        }

    for mapping in profile.get("text_mappings") or []:
        smart_path = tuple(str(part) for part in mapping.get("smart_object_path") or [])
        if smart_path:
            target = tuple(str(part) for part in mapping.get("target_path") or [])
            actual = inner_by_smart.get(smart_path, {}).get(target)
            if actual is None:
                errors.append("找不到智能对象内部图层路径: " + "/".join(smart_path) + " › " + "/".join(target))
            elif actual != "type":
                errors.append("智能对象内部目标不是文字层: " + "/".join(target))
        else:
            check_path(mapping.get("target_path"), "type")
    for mapping in profile.get("image_mappings") or []:
        check_path(mapping.get("target_path"))
    for mapping in profile.get("selector_mappings") or []:
        smart_path = tuple(str(part) for part in mapping.get("smart_object_path") or [])
        target_paths = inner_by_smart.get(smart_path, {}) if smart_path else paths
        parent = tuple(str(part) for part in mapping.get("parent_path") or [])
        if parent not in target_paths:
            errors.append("找不到图层切换父级路径: " + "/".join(smart_path) + (" › " if smart_path else "") + "/".join(parent))
        for paths_for_value in (mapping.get("value_mapping") or {}).values():
            for path in paths_for_value or []:
                target = tuple(str(part) for part in path or [])
                if target not in target_paths:
                    errors.append("找不到图层切换候选路径: " + "/".join(target))
    return list(dict.fromkeys(errors))
