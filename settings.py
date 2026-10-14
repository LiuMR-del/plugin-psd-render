"""用户设置持久化: ~/.plugin-psd-render/config.json

变体字段按「模版目录 -> SKU」两级记忆(variant_fields), 不同模版库、不同 SKU 各自记住配置。
"""

from __future__ import annotations

import json
import re
import os

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".plugin-psd-render")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

DEFAULTS = {
    "template_dir": "",
    "output_dir": "",
    "output_name_column": "",
    "last_excel_dir": "",
    "ai_base_url": "",
    "ai_models": ["gpt-5.5", "gpt-5.6-terra", "glm-5.3"],
    "ai_key_env": "PSD_RENDER_AI_API_KEY",
    "variant_fields": {},  # {模版目录: {sku: 变体字段列名}}
    "layer_aliases": {},   # {模版目录: {sku: {图层名: 对应 Excel 列名}}}, 未配置默认同名
    # {模版目录: {sku: {文件名: {selectors: [...]}}}}
    "group_configs": {},
    # {模版目录: {sku: {文件名: [{smart_path, text_path, column}]}}}
    "smart_object_aliases": {},
    # {模版目录: {sku: {文件名: [{smart_path, selectors}]}}}
    "smart_object_configs": {},
    # {模版目录: {sku: {文件名: [{layer_path, enabled, column, image_dir, mappings}]}}}
    "image_replacement_configs": {},
}


# 按「模版目录 -> SKU」记忆的配置项
PER_LIBRARY_KEYS = (
    "variant_fields", "layer_aliases", "group_configs",
    "smart_object_aliases", "smart_object_configs", "image_replacement_configs",
)


def load() -> dict:
    cfg = dict(DEFAULTS)
    for key in PER_LIBRARY_KEYS:
        cfg[key] = {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for key in DEFAULTS:
                if key in data and isinstance(data[key], type(DEFAULTS[key])):
                    cfg[key] = data[key]
    except (OSError, ValueError):
        pass
    # ai_key_env must be an environment-variable NAME; an older build stored
    # the API key itself here. Never keep (or re-save) a secret in config.
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", str(cfg.get("ai_key_env") or "")):
        cfg["ai_key_env"] = DEFAULTS["ai_key_env"]
    return cfg


def save(cfg: dict) -> bool:
    """Write config.json; False when it could not be written."""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        return False
    return True


def get_variant_field(cfg: dict, template_dir: str, sku: str) -> str:
    return cfg.get("variant_fields", {}).get(template_dir, {}).get(sku, "")


def set_variant_field(cfg: dict, template_dir: str, sku: str, field_name: str) -> None:
    by_dir = cfg.setdefault("variant_fields", {}).setdefault(template_dir, {})
    if field_name:
        by_dir[sku] = field_name
    else:
        by_dir.pop(sku, None)


def get_layer_aliases(cfg: dict, template_dir: str, sku: str) -> dict:
    return cfg.get("layer_aliases", {}).get(template_dir, {}).get(sku, {})


def set_layer_alias(cfg: dict, template_dir: str, sku: str, layer: str, column: str) -> None:
    by_sku = cfg.setdefault("layer_aliases", {}).setdefault(template_dir, {}).setdefault(sku, {})
    if column:
        by_sku[layer] = column
    else:
        by_sku.pop(layer, None)


def get_smart_object_alias(cfg: dict, template_dir: str, sku: str, filename: str,
                           smart_path: list[str], text_path: list[str]) -> str:
    entries = (
        cfg.get("smart_object_aliases", {})
        .get(template_dir, {})
        .get(sku, {})
        .get(filename, [])
    )
    smart_key = [str(part) for part in smart_path]
    text_key = [str(part) for part in text_path]
    for entry in entries:
        if (list(entry.get("smart_path") or []) == smart_key and
                list(entry.get("text_path") or []) == text_key):
            return str(entry.get("column") or "")
    return ""


def get_smart_object_aliases(
    cfg: dict, template_dir: str, sku: str, filename: str,
) -> list[dict]:
    entries = (
        cfg.get("smart_object_aliases", {})
        .get(template_dir, {})
        .get(sku, {})
        .get(filename, [])
    )
    return [
        {
            "smart_path": [str(part) for part in entry.get("smart_path") or []],
            "text_path": [str(part) for part in entry.get("text_path") or []],
            "column": str(entry.get("column") or ""),
        }
        for entry in entries
        if entry.get("smart_path") and entry.get("text_path")
        and str(entry.get("column") or "").strip()
    ]


def set_smart_object_alias(cfg: dict, template_dir: str, sku: str, filename: str,
                           smart_path: list[str], text_path: list[str], column: str) -> None:
    by_dir = cfg.setdefault("smart_object_aliases", {}).setdefault(template_dir, {})
    by_sku = by_dir.setdefault(sku, {})
    entries = list(by_sku.get(filename, []))
    smart_key = [str(part) for part in smart_path]
    text_key = [str(part) for part in text_path]
    entries = [
        entry for entry in entries
        if not (list(entry.get("smart_path") or []) == smart_key and
                list(entry.get("text_path") or []) == text_key)
    ]
    column = str(column or "").strip()
    if column:
        entries.append({
            "smart_path": smart_key,
            "text_path": text_key,
            "column": column,
        })
    if entries:
        by_sku[filename] = entries
    else:
        by_sku.pop(filename, None)
        if not by_sku:
            by_dir.pop(sku, None)
        if not by_dir:
            cfg["smart_object_aliases"].pop(template_dir, None)


def clear_sku_config(cfg: dict, template_dir: str, sku: str) -> None:
    """清除一个 SKU 的图层配置和切换模版字段。"""
    for key in PER_LIBRARY_KEYS:
        by_dir = cfg.get(key, {}).get(template_dir)
        if not isinstance(by_dir, dict):
            continue
        by_dir.pop(sku, None)
        if not by_dir:
            cfg[key].pop(template_dir, None)


def get_group_config(cfg: dict, template_dir: str, sku: str, filename: str) -> dict:
    return (
        cfg.get("group_configs", {})
        .get(template_dir, {})
        .get(sku, {})
        .get(filename, {"selectors": []})
    )


def _sanitize_selectors(config: dict) -> list[dict]:
    selectors = []
    for item in config.get("selectors", []):
        column = str(item.get("column") or "").strip()
        candidates = [
            {"path": list(candidate.get("path") or []),
             "value": str(candidate.get("value") or "").strip()}
            for candidate in item.get("candidates", [])
            if candidate.get("path") and str(candidate.get("value") or "").strip()
        ]
        aliases = {
            str(k): str(v).strip()
            for k, v in (item.get("relative_text_aliases") or {}).items()
            if str(v).strip()
        }
        enabled = bool(item.get("enabled") or column or candidates)
        if enabled or aliases:
            selectors.append({
                "parent": list(item.get("parent") or []),
                "enabled": enabled,
                "column": column,
                "candidates": candidates,
                "relative_text_aliases": aliases,
                "hide_values": [str(value).strip() for value in item.get("hide_values") or [] if str(value).strip()],
            })

    return selectors


def set_group_config(cfg: dict, template_dir: str, sku: str, filename: str, config: dict) -> None:
    selectors = _sanitize_selectors(config)
    by_file = cfg.setdefault("group_configs", {}).setdefault(template_dir, {}).setdefault(sku, {})
    if selectors:
        by_file[filename] = {"selectors": selectors}
    else:
        by_file.pop(filename, None)


def get_smart_object_config(cfg: dict, template_dir: str, sku: str, filename: str,
                            smart_path: list[str]) -> dict:
    entries = (
        cfg.get("smart_object_configs", {})
        .get(template_dir, {})
        .get(sku, {})
        .get(filename, [])
    )
    smart_key = [str(part) for part in smart_path]
    for entry in entries:
        if list(entry.get("smart_path") or []) == smart_key:
            return {"selectors": list(entry.get("selectors") or [])}
    return {"selectors": []}


def get_smart_object_configs(
    cfg: dict, template_dir: str, sku: str, filename: str,
) -> list[dict]:
    entries = (
        cfg.get("smart_object_configs", {})
        .get(template_dir, {})
        .get(sku, {})
        .get(filename, [])
    )
    return [
        {
            "smart_path": [str(part) for part in entry.get("smart_path") or []],
            "selectors": list(entry.get("selectors") or []),
        }
        for entry in entries
        if entry.get("smart_path")
    ]


def set_smart_object_config(cfg: dict, template_dir: str, sku: str, filename: str,
                            smart_path: list[str], config: dict) -> None:
    by_dir = cfg.setdefault("smart_object_configs", {}).setdefault(template_dir, {})
    by_sku = by_dir.setdefault(sku, {})
    entries = list(by_sku.get(filename, []))
    smart_key = [str(part) for part in smart_path]
    entries = [
        entry for entry in entries
        if list(entry.get("smart_path") or []) != smart_key
    ]
    selectors = _sanitize_selectors(config)
    if selectors:
        entries.append({"smart_path": smart_key, "selectors": selectors})
    if entries:
        by_sku[filename] = entries
    else:
        by_sku.pop(filename, None)
        if not by_sku:
            by_dir.pop(sku, None)
        if not by_dir:
            cfg["smart_object_configs"].pop(template_dir, None)


def get_image_replacement_configs(
    cfg: dict, template_dir: str, sku: str, filename: str,
) -> list[dict]:
    entries = (
        cfg.get("image_replacement_configs", {})
        .get(template_dir, {})
        .get(sku, {})
        .get(filename, [])
    )
    return [
        {
            "layer_path": [str(part) for part in entry.get("layer_path") or []],
            "enabled": bool(entry.get("enabled")),
            "column": str(entry.get("column") or ""),
            "image_dir": str(entry.get("image_dir") or ""),
            "mappings": [
                {
                    "value": str(item.get("value") or ""),
                    "file": str(item.get("file") or ""),
                }
                for item in entry.get("mappings") or []
                if str(item.get("value") or "").strip()
                and str(item.get("file") or "").strip()
            ],
        }
        for entry in entries
        if entry.get("layer_path")
    ]


def get_image_replacement_config(
    cfg: dict, template_dir: str, sku: str, filename: str, layer_path: list[str],
) -> dict:
    key = [str(part) for part in layer_path]
    for entry in get_image_replacement_configs(cfg, template_dir, sku, filename):
        if entry["layer_path"] == key:
            return entry
    return {
        "layer_path": key,
        "enabled": False,
        "column": "",
        "image_dir": "",
        "mappings": [],
    }


def set_image_replacement_config(
    cfg: dict, template_dir: str, sku: str, filename: str,
    layer_path: list[str], config: dict | None,
) -> None:
    by_dir = cfg.setdefault("image_replacement_configs", {}).setdefault(template_dir, {})
    by_sku = by_dir.setdefault(sku, {})
    entries = list(by_sku.get(filename, []))
    key = [str(part) for part in layer_path]
    entries = [
        entry for entry in entries
        if [str(part) for part in entry.get("layer_path") or []] != key
    ]
    if config is not None:
        entries.append({
            "layer_path": key,
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
        })
    if entries:
        by_sku[filename] = entries
    else:
        by_sku.pop(filename, None)
        if not by_sku:
            by_dir.pop(sku, None)
        if not by_dir:
            cfg["image_replacement_configs"].pop(template_dir, None)
