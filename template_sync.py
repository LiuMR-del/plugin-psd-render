"""Template configuration sync planning helpers."""

from __future__ import annotations


def _path(layer: dict) -> list[str]:
    parts = layer.get("path_parts")
    if isinstance(parts, list) and parts:
        return [str(part) for part in parts]
    raw = str(layer.get("path") or layer.get("name") or "")
    return [part for part in raw.split("/") if part]


def _selector(config: dict, parent: list[str]) -> dict:
    key = tuple(parent)
    for item in config.get("selectors") or []:
        if tuple(str(part) for part in item.get("parent") or []) == key:
            return item
    return {}


def _smart_maps(layers: list[dict]) -> tuple[dict, dict]:
    smart_layers = {}
    smart_text_paths = {}
    for layer in layers:
        if layer.get("kind") != "smartobject":
            continue
        key = tuple(_path(layer))
        smart_layers[key] = layer
        info = dict(layer.get("smart_object") or {})
        smart_text_paths[key] = {
            tuple(_path(inner))
            for inner in info.get("layers") or []
            if inner.get("kind") == "type"
        }
    return smart_layers, smart_text_paths


def _matching_relative_paths(
    layers: list[dict], parent: list[str], relative_path: str,
) -> list[list[str]]:
    parent_key = tuple(parent)
    direct_groups = {
        tuple(_path(layer))
        for layer in layers
        if layer.get("kind") == "group"
        and len(_path(layer)) == len(parent) + 1
        and tuple(_path(layer)[:len(parent)]) == parent_key
    }
    rel = tuple(part for part in str(relative_path).split("/") if part)
    matches = []
    for layer in layers:
        if layer.get("kind") != "type":
            continue
        parts = tuple(_path(layer))
        for group in direct_groups:
            if parts == group + rel:
                matches.append(list(parts))
                break
    return matches


def collect_text_sync_entries(
    layers: list[dict], root_aliases: dict, group_config: dict,
    smart_aliases: list[dict], smart_configs: list[dict],
) -> list[dict]:
    """Flatten all configured text mappings for one source template."""
    entries: list[dict] = []
    for layer in layers:
        parts = _path(layer)
        if layer.get("kind") != "type" or len(parts) != 1:
            continue
        column = str(root_aliases.get(parts[0]) or "").strip()
        if column:
            entries.append({
                "scope": "root", "layer_path": parts,
                "column": column, "source_valid": True,
            })

    for selector in group_config.get("selectors") or []:
        parent = [str(part) for part in selector.get("parent") or []]
        for relative_path, raw_column in (
            selector.get("relative_text_aliases") or {}
        ).items():
            column = str(raw_column or "").strip()
            if not column:
                continue
            matches = _matching_relative_paths(layers, parent, str(relative_path))
            entries.append({
                "scope": "group", "parent": parent,
                "relative_path": str(relative_path), "match_paths": matches,
                "column": column, "source_valid": bool(matches),
            })

    smart_layers, smart_text_paths = _smart_maps(layers)
    for alias in smart_aliases:
        smart_path = [str(part) for part in alias.get("smart_path") or []]
        text_path = [str(part) for part in alias.get("text_path") or []]
        column = str(alias.get("column") or "").strip()
        if not column:
            continue
        smart_key = tuple(smart_path)
        source_valid = (
            smart_key in smart_layers
            and tuple(text_path) in smart_text_paths.get(smart_key, set())
        )
        entries.append({
            "scope": "smart", "smart_path": smart_path,
            "text_path": text_path, "column": column,
            "source_valid": source_valid,
        })

    for smart_config in smart_configs:
        smart_path = [str(part) for part in smart_config.get("smart_path") or []]
        smart_key = tuple(smart_path)
        inner_layers = list(
            (smart_layers.get(smart_key, {}).get("smart_object") or {}).get("layers") or []
        )
        for selector in smart_config.get("selectors") or []:
            parent = [str(part) for part in selector.get("parent") or []]
            for relative_path, raw_column in (
                selector.get("relative_text_aliases") or {}
            ).items():
                column = str(raw_column or "").strip()
                if not column:
                    continue
                matches = _matching_relative_paths(
                    inner_layers, parent, str(relative_path),
                )
                entries.append({
                    "scope": "smart_group", "smart_path": smart_path,
                    "parent": parent, "relative_path": str(relative_path),
                    "match_paths": matches, "column": column,
                    "source_valid": bool(matches),
                })
    return entries


def _smart_alias_index(entries: list[dict]) -> dict:
    return {
        (
            tuple(str(part) for part in entry.get("smart_path") or []),
            tuple(str(part) for part in entry.get("text_path") or []),
        ): str(entry.get("column") or "").strip()
        for entry in entries
    }


def _smart_config_index(entries: list[dict]) -> dict:
    return {
        tuple(str(part) for part in entry.get("smart_path") or []): entry
        for entry in entries
    }


def text_entry_display_path(entry: dict) -> str:
    scope = entry.get("scope")
    if scope == "root":
        return "/".join(entry.get("layer_path") or [])
    if scope == "group":
        prefix = "/".join(entry.get("parent") or []) or "顶层候选组"
        return f"{prefix}/*/{entry.get('relative_path') or ''}"
    smart_path = "/".join(entry.get("smart_path") or [])
    if scope == "smart":
        return f"{smart_path} › {'/'.join(entry.get('text_path') or [])}"
    prefix = "/".join(entry.get("parent") or []) or "内部顶层候选组"
    return f"{smart_path} › {prefix}/*/{entry.get('relative_path') or ''}"


def plan_text_config_sync(
    source_entries: list[dict], target_layers: list[dict],
    target_root_aliases: dict, target_group_config: dict,
    target_smart_aliases: list[dict], target_smart_configs: list[dict],
) -> dict:
    """Plan text mapping synchronization without mutating either template config."""
    target_text_paths = {
        tuple(_path(layer)) for layer in target_layers if layer.get("kind") == "type"
    }
    _, target_smart_text_paths = _smart_maps(target_layers)
    smart_alias_index = _smart_alias_index(target_smart_aliases)
    smart_config_index = _smart_config_index(target_smart_configs)
    counts = {"add": 0, "overwrite": 0, "unchanged": 0, "ignored": 0}
    actions = []

    for source in source_entries:
        entry = dict(source)
        scope = entry.get("scope")
        status = "ignored"
        reason = ""
        current = ""
        eligible = False

        if not entry.get("source_valid"):
            reason = "源模板中已找不到对应文字图层"
        elif scope == "root":
            layer_path = tuple(entry.get("layer_path") or [])
            layer_name = layer_path[-1]
            eligible = any(path and path[-1] == layer_name for path in target_text_paths)
            current = str(target_root_aliases.get(layer_path[-1]) or "").strip()
            if eligible:
                status = "unchanged"
                reason = "同一 SKU 的普通文字映射已自动共享"
        elif scope == "group":
            eligible = any(
                tuple(path) in target_text_paths for path in entry.get("match_paths") or []
            )
            selector = _selector(target_group_config, entry.get("parent") or [])
            current = str(
                (selector.get("relative_text_aliases") or {}).get(
                    entry.get("relative_path"), "",
                )
            ).strip()
            if not eligible:
                rel_parts = [
                    part for part in str(entry.get("relative_path") or "").split("/")
                    if part
                ]
                leaf = rel_parts[-1] if len(rel_parts) == 1 else ""
                has_same_name = bool(leaf) and any(
                    path and path[-1] == leaf for path in target_text_paths
                )
                shared_column = str(target_root_aliases.get(leaf) or leaf).strip()
                if has_same_name and shared_column == entry.get("column"):
                    eligible = True
                    status = "unchanged"
                    reason = "目标文字层级不同，但同 SKU 共享或同名映射已生效"
        elif scope == "smart":
            smart_key = tuple(entry.get("smart_path") or [])
            text_key = tuple(entry.get("text_path") or [])
            eligible = text_key in target_smart_text_paths.get(smart_key, set())
            current = smart_alias_index.get((smart_key, text_key), "")
        elif scope == "smart_group":
            smart_key = tuple(entry.get("smart_path") or [])
            eligible = any(
                tuple(path) in target_smart_text_paths.get(smart_key, set())
                for path in entry.get("match_paths") or []
            )
            config = smart_config_index.get(smart_key, {})
            selector = _selector(config, entry.get("parent") or [])
            current = str(
                (selector.get("relative_text_aliases") or {}).get(
                    entry.get("relative_path"), "",
                )
            ).strip()

        if entry.get("source_valid") and not eligible:
            reason = "目标模板缺少对应文字图层"
        elif eligible and scope != "root" and status != "unchanged":
            if not current:
                status = "add"
            elif current == entry.get("column"):
                status = "unchanged"
            else:
                status = "overwrite"

        counts[status] += 1
        actions.append({
            "status": status, "reason": reason,
            "entry": entry, "display_path": text_entry_display_path(entry),
        })

    return {
        "actions": actions, "counts": counts,
        "actionable": counts["add"] + counts["overwrite"],
    }
