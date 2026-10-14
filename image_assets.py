"""External raster asset discovery and safe path resolution."""

from __future__ import annotations

import os

SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg"}


def _layer_path(layer: dict) -> list[str]:
    parts = layer.get("path_parts")
    if isinstance(parts, list) and parts:
        return [str(part) for part in parts]
    path = str(layer.get("path") or layer.get("name") or "")
    return [part for part in path.split("/") if part]


def is_image_replacement_layer(layer: dict | None) -> bool:
    """Return whether a scanned PSD layer supports whole-image replacement."""
    if not layer:
        return False
    kind = str(layer.get("kind") or "")
    if kind == "pixel":
        return True
    if kind != "smartobject":
        return False
    info = dict(layer.get("smart_object") or {})
    file_type = str(info.get("file_type") or "").strip().lower().lstrip(".")
    filename_ext = os.path.splitext(str(info.get("filename") or ""))[1].lower()
    return file_type in {"png", "jpg", "jpeg"} or filename_ext in SUPPORTED_EXTENSIONS


def _normalized_image_config(config: dict) -> dict:
    return {
        "layer_path": [str(part) for part in config.get("layer_path") or []],
        "enabled": bool(config.get("enabled")),
        "column": str(config.get("column") or "").strip(),
        "image_dir": str(config.get("image_dir") or "").strip(),
        "mappings": [
            {
                "value": str(item.get("value") or "").strip(),
                "file": str(item.get("file") or "").strip().replace("\\", "/"),
            }
            for item in config.get("mappings") or []
            if str(item.get("value") or "").strip()
            and str(item.get("file") or "").strip()
        ],
    }


def _config_signature(config: dict) -> tuple:
    normalized = _normalized_image_config(config)
    mappings = tuple(sorted(
        (item["value"], item["file"]) for item in normalized["mappings"]
    ))
    return (
        normalized["enabled"], normalized["column"],
        normalized["image_dir"], mappings,
    )


def plan_image_config_sync(
    source_configs: list[dict], source_layers: list[dict],
    target_layers: list[dict], target_configs: list[dict],
) -> dict:
    """Plan an exact-layer-path image configuration sync without mutating inputs."""
    source_index = {tuple(_layer_path(layer)): layer for layer in source_layers}
    target_index = {tuple(_layer_path(layer)): layer for layer in target_layers}
    target_config_index = {
        tuple(str(part) for part in config.get("layer_path") or []): config
        for config in target_configs
    }
    actions: list[dict] = []
    counts = {"add": 0, "overwrite": 0, "unchanged": 0, "ignored": 0}

    for raw_config in source_configs:
        config = _normalized_image_config(raw_config)
        layer_path = config["layer_path"]
        key = tuple(layer_path)
        status = "ignored"
        reason = ""
        source_layer = source_index.get(key)
        target_layer = target_index.get(key)
        current = target_config_index.get(key)

        if not source_layer:
            reason = "源模板中已找不到该图层"
        elif not is_image_replacement_layer(source_layer):
            reason = "源模板中的图层类型不支持图片替换"
        elif not target_layer:
            reason = "目标模板缺少该图层"
        elif not is_image_replacement_layer(target_layer):
            reason = "目标模板中的图层类型不支持图片替换"
        elif current is None:
            status = "add"
        elif _config_signature(current) == _config_signature(config):
            status = "unchanged"
        else:
            status = "overwrite"

        counts[status] += 1
        actions.append({
            "layer_path": list(layer_path),
            "status": status,
            "reason": reason,
            "config": config,
        })

    return {
        "actions": actions,
        "counts": counts,
        "actionable": counts["add"] + counts["overwrite"],
    }


def scan_image_files(image_dir: str) -> list[str]:
    """Return supported files below *image_dir* as stable POSIX-style paths."""
    raw_root = str(image_dir or "").strip()
    if not raw_root:
        raise ValueError("素材目录不能为空")
    root = os.path.realpath(os.path.abspath(raw_root))
    if not os.path.isdir(root):
        raise ValueError(f"素材目录不存在: {root}")

    files: list[str] = []
    for current, dirs, names in os.walk(root):
        dirs[:] = sorted(
            (name for name in dirs if not name.startswith(".")),
            key=str.casefold,
        )
        for name in sorted(names, key=str.casefold):
            if name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() not in SUPPORTED_EXTENSIONS:
                continue
            path = os.path.join(current, name)
            files.append(os.path.relpath(path, root).replace(os.sep, "/"))
    return files


def resolve_image_path(image_dir: str, relative_file: str) -> str:
    """Resolve a configured relative path without allowing directory escape."""
    raw_root = str(image_dir or "").strip()
    root = os.path.realpath(os.path.abspath(raw_root)) if raw_root else ""
    relative = str(relative_file or "").strip().replace("\\", "/")
    if not root or not relative or os.path.isabs(relative):
        raise ValueError(f"无效素材相对路径: {relative or '空'}")

    candidate = os.path.realpath(os.path.abspath(os.path.join(root, *relative.split("/"))))
    try:
        inside_root = os.path.commonpath([root, candidate]) == root
    except ValueError:
        inside_root = False
    if not inside_root:
        raise ValueError(f"素材路径越出配置目录: {relative}")
    if os.path.splitext(candidate)[1].lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"不支持的素材格式: {relative}")
    return candidate


def mapping_index(config: dict) -> tuple[dict[str, str], list[str]]:
    """Build an exact value-to-file index and return duplicate values."""
    index: dict[str, str] = {}
    duplicates: list[str] = []
    for item in config.get("mappings", []):
        value = str(item.get("value") or "").strip()
        filename = str(item.get("file") or "").strip().replace("\\", "/")
        if not value or not filename:
            continue
        if value in index:
            duplicates.append(value)
            continue
        index[value] = filename
    return index, list(dict.fromkeys(duplicates))


def resolve_mapped_image(config: dict, excel_value: str) -> str:
    """Resolve one non-empty Excel value through an explicit layer mapping."""
    value = str(excel_value or "").strip()
    index, duplicates = mapping_index(config)
    if duplicates:
        raise ValueError("图片映射存在重复 Excel 值: " + "、".join(duplicates))
    if value not in index:
        raise ValueError(f"Excel 值「{value}」没有配置图片映射")
    path = resolve_image_path(config.get("image_dir", ""), index[value])
    if not os.path.isfile(path):
        raise ValueError(f"映射的素材文件不存在: {index[value]}")
    return path
