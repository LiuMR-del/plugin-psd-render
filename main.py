"""PSD 批量生图 — Flet GUI 与流程编排(入口).

双页结构:
- 配置页: 模版目录 -> SKU 维度卡片清单(左侧模版缩略图+文件名, 多模版时可填「切换模版字段」;
          右侧树形图层配置, 支持按 Excel 字段切换同一父级下的可选图层/图层组、按文字字段替换文案)
          + 输出目录; 选择 Excel 后经预检确认直接进入任务处理页
- 任务处理页: 任务进度 + 处理明细, 完成后可打开输出目录 / 返回配置页

渲染循环跑后台线程, UI 通过 page.update() 刷进度。
"""

from __future__ import annotations

import asyncio
import copy
import csv
import json
import os
import shutil
import subprocess
import sys
import threading
import traceback
import uuid
from datetime import date, datetime, timezone

import flet as ft

import ai_client
import ai_mapping
import ai_runtime
import custom_fields
import excel_data
import excel_marker
import font_installer
import image_assets
import library_cleanup
import order_intake
import order_analysis
import order_pipeline
import output_layout
import renderer
import settings
import template_profiles
import template_sync
import templates
import text_rules

VERSION = "v1.6"
MAX_LOG_LINES = 500
THUMB_SINGLE = 160  # 单模版缩略图边长
THUMB_MULTI = 120   # 多模版缩略图边长
SMART_TARGET_MARKER = "__smart_object_text_target_v1__"


def layer_kind_label(kind: str) -> str:
    labels = {
        "group": "组",
        "type": "文字",
        "pixel": "像素",
        "shape": "形状",
        "smartobject": "智能对象",
    }
    return labels.get(kind, kind or "图层")


def layer_kind_status_label(kind: str, visible: bool) -> str:
    label = layer_kind_label(kind)
    if not label:
        return ""
    return label + ("" if visible else " · 隐藏")


def is_raster_image_smart_object(layer: dict) -> bool:
    return (
        layer.get("kind") == "smartobject"
        and image_assets.is_image_replacement_layer(layer)
    )


def open_in_file_manager(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.run(["open", path], check=False)
    else:
        subprocess.run(["xdg-open", path], check=False)


def existing_directory(path: str | None) -> str | None:
    """Return a usable local initial directory, ignoring stale saved paths."""
    value = str(path or "").strip()
    return value if value and os.path.isdir(value) else None


def excel_output_directory(path: str) -> str:
    """Return the default output directory for a successfully imported order file."""
    return os.path.dirname(os.path.abspath(str(path or "")))


def pick_windows_directory(dialog_title: str, initial_directory: str | None = None) -> str:
    """Use Windows' native folder dialog without depending on Flet's picker bridge."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        root.update_idletasks()
        options = {
            "parent": root,
            "title": dialog_title,
            "mustexist": True,
        }
        initial = existing_directory(initial_directory)
        if initial:
            options["initialdir"] = initial
        selected = filedialog.askdirectory(**options)
        return os.path.normpath(selected) if selected else ""
    finally:
        root.destroy()


def layer_path_parts(layer: dict) -> list[str]:
    parts = layer.get("path_parts")
    if isinstance(parts, list) and parts:
        return [str(p) for p in parts]
    path = str(layer.get("path") or layer.get("name") or "")
    return [p for p in path.split("/") if p]


def path_key(parts: list[str]) -> str:
    return "/".join(parts)


def is_under_path(parts: list[str], parent: list[str]) -> bool:
    return len(parts) > len(parent) and parts[:len(parent)] == parent


def relative_path(parts: list[str], parent: list[str]) -> str:
    return path_key(parts[len(parent):])


def display_path(parts: list[str]) -> str:
    return path_key(parts) if parts else "顶层"


def direct_child_layers(t: templates.Template, parent: list[str]) -> list[dict]:
    return direct_child_layer_items(t.all_layers, parent)


def direct_child_layer_items(layers: list[dict], parent: list[str]) -> list[dict]:
    children = []
    child_len = len(parent) + 1
    for layer in layers:
        parts = layer_path_parts(layer)
        if len(parts) == child_len and parts[:len(parent)] == parent:
            children.append(layer)
    return children


def child_parent_path(parts: list[str]) -> list[str]:
    return parts[:-1]


def has_child_layers(t: templates.Template, parent: list[str]) -> bool:
    return any(True for _ in direct_child_layers(t, parent))


def has_child_layer_items(layers: list[dict], parent: list[str]) -> bool:
    return any(True for _ in direct_child_layer_items(layers, parent))


def smart_target_parts(smart_path: list[str], text_path: list[str]) -> list[str]:
    return [
        SMART_TARGET_MARKER,
        json.dumps([str(part) for part in smart_path], ensure_ascii=False, separators=(",", ":")),
        *[str(part) for part in text_path],
    ]


def parse_smart_target_parts(parts: list[str]) -> tuple[list[str], list[str]] | None:
    if len(parts) < 3 or parts[0] != SMART_TARGET_MARKER:
        return None
    try:
        smart_path = json.loads(parts[1])
    except (TypeError, ValueError):
        return None
    if not isinstance(smart_path, list) or not smart_path:
        return None
    return [str(part) for part in smart_path], [str(part) for part in parts[2:]]


def _smart_results_by_path(items: list[dict], value_key: str) -> dict[tuple[str, ...], object]:
    """Return the final generated result for each smart-object inner path."""
    results: dict[tuple[str, ...], object] = {}
    for item in items:
        inner_path = tuple(str(part) for part in item.get("path") or [])
        if not inner_path:
            continue
        value = item.get(value_key)
        results[inner_path] = bool(value) if value_key == "visible" else value
    return results


def shared_smart_object_task_differences(
    left: dict, right: dict,
) -> list[tuple[str, tuple[str, ...], object, object]]:
    """Compare two references to one embedded smart object by inner layer path."""
    differences: list[tuple[str, tuple[str, ...], object, object]] = []
    result_groups = (
        ("图层显隐", "visibilityActions", "visible"),
        ("文字替换", "pathReplacements", "value"),
    )
    for kind, items_key, value_key in result_groups:
        left_results = _smart_results_by_path(list(left.get(items_key) or []), value_key)
        right_results = _smart_results_by_path(list(right.get(items_key) or []), value_key)
        for inner_path, left_value in left_results.items():
            if inner_path not in right_results:
                continue
            right_value = right_results[inner_path]
            if left_value != right_value:
                differences.append((kind, inner_path, left_value, right_value))
    return differences


def _format_smart_result(kind: str, value: object) -> str:
    if kind == "图层显隐":
        return "显示" if value else "隐藏"
    text = str(value if value is not None else "")
    return "替换为空文本" if not text else f"替换为「{text}」"


def format_shared_smart_object_conflict(
    left: dict,
    right: dict,
    differences: list[tuple[str, tuple[str, ...], object, object]],
) -> str:
    """Build a readable preflight error for conflicting shared-object results."""
    left_path = display_path([str(part) for part in left.get("smartObjectPath") or []])
    right_path = display_path([str(part) for part in right.get("smartObjectPath") or []])
    lines = [
        "共享智能对象配置冲突",
        "同一个智能对象内容被以下两个图层共同引用，但本行的生成结果不一致：",
        f"  引用 A：{left_path}",
        f"  引用 B：{right_path}",
        "差异位置：",
    ]
    for index, (kind, inner_path, left_value, right_value) in enumerate(differences, start=1):
        lines.extend([
            f"  {index}. {kind} · 内部图层「{display_path(list(inner_path))}」",
            f"     - 引用 A：{_format_smart_result(kind, left_value)}",
            f"     - 引用 B：{_format_smart_result(kind, right_value)}",
        ])
    lines.append("处理方法：打开该 SKU 的“编辑配置”，将上述位置在两个引用中的配置调整为一致。")
    return "\n".join(lines)


def _merge_smart_task_items(
    target: dict, source: dict, items_key: str, value_key: str,
) -> None:
    """Merge source results for paths that target does not explicitly configure."""
    target_items = list(target.get(items_key) or [])
    source_items = list(source.get(items_key) or [])
    target_paths = set(_smart_results_by_path(target_items, value_key))
    source_paths = set(_smart_results_by_path(source_items, value_key))
    added_paths = source_paths - target_paths
    if added_paths:
        target_items.extend(
            item for item in source_items
            if tuple(str(part) for part in item.get("path") or []) in added_paths
        )
    target[items_key] = target_items


def merge_shared_smart_object_tasks(target: dict, source: dict) -> None:
    """Combine compatible explicit results from two references to shared content."""
    _merge_smart_task_items(target, source, "visibilityActions", "visible")
    _merge_smart_task_items(target, source, "pathReplacements", "value")


def consolidate_smart_object_tasks(tasks: list[dict]) -> list[dict]:
    by_object_id: dict[str, dict] = {}
    for task in tasks:
        smart_path = [str(part) for part in task.get("smartObjectPath") or []]
        object_id = str(task.get("smartObjectId") or "path:" + path_key(smart_path))
        normalized = {
            "smartObjectId": object_id,
            "smartObjectPath": smart_path,
            "visibilityActions": list(task.get("visibilityActions") or []),
            "pathReplacements": list(task.get("pathReplacements") or []),
        }
        existing = by_object_id.get(object_id)
        if existing is not None:
            differences = shared_smart_object_task_differences(existing, normalized)
            if differences:
                raise ValueError(format_shared_smart_object_conflict(
                    existing, normalized, differences,
                ))
            merge_shared_smart_object_tasks(existing, normalized)
            continue
        by_object_id[object_id] = normalized
    return [
        task for task in by_object_id.values()
        if task["visibilityActions"] or task["pathReplacements"]
    ]


def consolidate_image_replacement_actions(actions: list[dict]) -> list[dict]:
    """Run one compatible replacement for references sharing smart-object content."""
    consolidated: list[dict] = []
    by_object_id: dict[str, dict] = {}
    for action in actions:
        object_id = str(action.get("smartObjectId") or "")
        if not object_id:
            consolidated.append(action)
            continue
        existing = by_object_id.get(object_id)
        if existing is None:
            by_object_id[object_id] = action
            consolidated.append(action)
            continue
        left_source = os.path.normcase(os.path.abspath(str(existing.get("sourcePath") or "")))
        right_source = os.path.normcase(os.path.abspath(str(action.get("sourcePath") or "")))
        if left_source != right_source:
            raise ValueError(
                "共享图片智能对象配置冲突: 图层「" +
                display_path([str(part) for part in existing.get("path") or []]) +
                "」与「" + display_path([str(part) for part in action.get("path") or []]) +
                "」引用同一内容，但映射到了不同素材"
            )
    return consolidated


def selector_match(config: dict, parent: list[str]) -> dict:
    key = path_key(parent)
    for item in config.get("selectors", []):
        if path_key(list(item.get("parent") or [])) == key:
            return item
    return {
        "parent": parent,
        "enabled": False,
        "column": "",
        "candidates": [],
        "relative_text_aliases": {},
    }


def set_selector(config: dict, parent: list[str], selector: dict) -> dict:
    key = path_key(parent)
    selectors = list(config.get("selectors", []))
    for i, item in enumerate(selectors):
        if path_key(list(item.get("parent") or [])) == key:
            selectors[i] = selector
            break
    else:
        selectors.append(selector)
    config["selectors"] = selectors
    return config


def set_selector_column(config: dict, parent: list[str], column: str) -> dict:
    selector = dict(selector_match(config, parent))
    selector["parent"] = parent
    selector["enabled"] = True
    selector["column"] = column
    return set_selector(config, parent, selector)


def set_selector_enabled(config: dict, parent: list[str], enabled: bool) -> dict:
    selector = dict(selector_match(config, parent))
    selector["parent"] = parent
    selector["enabled"] = enabled
    if not enabled:
        selector["column"] = ""
        selector["candidates"] = []
    return set_selector(config, parent, selector)


def set_candidate_value(config: dict, parent: list[str], candidate_path: list[str], value: str) -> dict:
    selector = dict(selector_match(config, parent))
    selector["parent"] = parent
    selector["enabled"] = True
    candidates = [
        dict(item) for item in selector.get("candidates", [])
        if path_key(list(item.get("path") or [])) != path_key(candidate_path)
    ]
    if value:
        candidates.append({"path": candidate_path, "value": value})
    selector["candidates"] = candidates
    return set_selector(config, parent, selector)


def set_selector_relative_alias(config: dict, parent: list[str], rel: str, column: str) -> dict:
    selector = dict(selector_match(config, parent))
    selector["parent"] = parent
    aliases = dict(selector.get("relative_text_aliases", {}))
    if column:
        aliases[rel] = column
    else:
        aliases.pop(rel, None)
    selector["relative_text_aliases"] = aliases
    return set_selector(config, parent, selector)


def relative_text_alias_for_path(config: dict, text_path: list[str]) -> str:
    """Resolve the per-template alias used by a text layer inside a candidate group."""
    parts = [str(part) for part in text_path]
    if len(parts) < 2:
        return ""
    parent = parts[:-2]
    relative_path = parts[-1]
    selector = selector_match(config, parent)
    return str(
        (selector.get("relative_text_aliases") or {}).get(relative_path, "")
    ).strip()


def candidate_value(selector: dict, candidate_path: list[str]) -> str:
    key = path_key(candidate_path)
    for item in selector.get("candidates", []):
        if path_key(list(item.get("path") or [])) == key:
            return str(item.get("value") or "")
    return ""


def build_selector_actions(
    selectors: list[dict], values: dict[str, str], text_paths: set[str],
) -> tuple[list[dict], list[dict]]:
    visibility_actions: list[dict] = []
    path_replacements: list[dict] = []
    for selector in selectors:
        excel_value = values.get(selector["column"], "")
        if not excel_value:
            continue
        hide_values = {
            str(value).strip().casefold()
            for value in selector.get("hide_values") or []
        }
        if excel_value.strip().casefold() in hide_values or excel_value.strip().casefold() in {"no", "none", "null", "无", "隐藏", "hide", "hidden"}:
            if selector["parent"]:
                visibility_actions.append({"path": selector["parent"], "visible": False})
            continue
        matched = [
            candidate for candidate in selector["candidates"]
            if str(candidate["value"]).strip().casefold() == excel_value.strip().casefold()
        ]
        if not matched:
            continue
        if selector["parent"]:
            visibility_actions.append({"path": selector["parent"], "visible": True})
        for candidate in selector["candidates"]:
            visibility_actions.append({"path": candidate["path"], "visible": False})
        for candidate in matched:
            visibility_actions.append({"path": candidate["path"], "visible": True})
            for rel, alias_column in selector["relative_text_aliases"].items():
                alias_column = str(alias_column or "").strip()
                value = values.get(alias_column, "") if alias_column else ""
                if not value:
                    continue
                rel_parts = [part for part in str(rel).split("/") if part]
                if not rel_parts:
                    continue
                path = list(candidate["path"]) + rel_parts
                if path_key(path) not in text_paths:
                    continue
                path_replacements.append({"path": path, "value": value})
    return visibility_actions, path_replacements


def collect_candidate_relative_text_layers(t: templates.Template, parent: list[str]) -> list[str]:
    candidate_groups = [
        layer_path_parts(layer)
        for layer in direct_child_layers(t, parent)
        if layer.get("kind") == "group"
    ]
    rels: list[str] = []
    for layer in t.all_layers:
        if layer.get("kind") != "type":
            continue
        parts = layer_path_parts(layer)
        parents = [g for g in candidate_groups if is_under_path(parts, g)]
        if not parents:
            continue
        parent = max(parents, key=len)
        rels.append(relative_path(parts, parent))
    return list(dict.fromkeys(rels))



def main(page: ft.Page):
    page.title = f"PSD 批量生图 {VERSION}"
    page.window.width = 1080
    page.window.height = 860
    page.padding = 20
    # Use theme roles instead of fixed light-palette values so the UI follows
    # macOS appearance changes without leaving pastel surfaces and borders
    # behind in dark mode.
    page.theme_mode = ft.ThemeMode.SYSTEM
    page.theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO_600)
    page.dark_theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO_300)

    cfg = settings.load()
    state = {
        "index": {},        # sku -> list[Template]
        "stats": None,
        "excel": None,      # ExcelData
        "excel_path": "",
        "orders": [],
        "order_summary": None,
        "profile_store": {},
        "analysis_tasks": {},  # key -> {task_id, cancel_event, started_at, deadline_at}
        "analysis_lock": threading.RLock(),
        "analysis_max_concurrency": 1,
        "order_filter": "all",
        "selected_rows": set(),  # 勾选的可生成订单行号；为空 = 生成全部可生成订单
        "running": False,
        "editing": set(),   # 处于字段编辑态的 sku
        "expanded": set(),   # 本次运行内手动展开的图层组: sku/filename/path
        "expanded_templates": set(),  # 当前运行内展开的 SKU/模版配置区域
        "seen_template_sections": set(),
        "image_dir_callback": None,
    }

    # ---------- 通用弹窗 ----------
    def close_dialog(dlg: ft.AlertDialog):
        page.close(dlg)

    def alert(title: str, message: str):
        dlg = ft.AlertDialog(title=ft.Text(title), content=ft.Text(message, selectable=True))
        dlg.actions = [ft.TextButton(
            "关闭",
            height=36,
            style=text_button_style,
            on_click=lambda e: close_dialog(dlg),
        )]
        page.open(dlg)

    # ================= 配置页 =================
    button_shape = ft.RoundedRectangleBorder(radius=6)
    action_button_style = ft.ButtonStyle(
        shape=button_shape,
        padding=ft.padding.symmetric(horizontal=20),
        text_style=ft.TextStyle(size=14, weight=ft.FontWeight.W_600),
        icon_size=18,
    )
    compact_button_style = ft.ButtonStyle(
        shape=button_shape,
        padding=ft.padding.symmetric(horizontal=12),
        text_style=ft.TextStyle(size=13, weight=ft.FontWeight.W_600),
        icon_size=16,
    )
    danger_filled_button_style = ft.ButtonStyle(
        shape=button_shape,
        padding=ft.padding.symmetric(horizontal=12),
        text_style=ft.TextStyle(size=13, weight=ft.FontWeight.W_600),
        icon_size=16,
        bgcolor=ft.Colors.ERROR,
        color=ft.Colors.ON_ERROR,
    )
    text_button_style = ft.ButtonStyle(
        shape=button_shape,
        padding=ft.padding.symmetric(horizontal=12),
        text_style=ft.TextStyle(size=13, weight=ft.FontWeight.W_600),
        icon_size=16,
    )
    icon_button_style = ft.ButtonStyle(
        shape=button_shape,
        padding=0,
        icon_size=16,
    )
    template_dir_field = ft.TextField(label="模版目录", value=cfg.get("template_dir", ""),
                                      read_only=True, expand=True, dense=True)
    template_status = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
    template_dir_name_text = ft.Text(os.path.basename(template_dir_field.value) or "尚未选择",
                                     weight=ft.FontWeight.W_600)
    template_library_path_status = ft.Text("", size=12, selectable=True,
                                           color=ft.Colors.ON_SURFACE_VARIANT)
    sku_list_view = ft.ListView(expand=True, spacing=8, padding=8)
    order_list_view = ft.ListView(expand=True, spacing=6, padding=8)
    order_file_status = ft.Text("尚未导入订单 Excel", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
    order_summary_text = ft.Text(
        "上传订单后，系统会按 SKU + 严格方向尺寸匹配模板。",
        size=12,
        selectable=True,
    )
    upload_missing_template_btn = ft.OutlinedButton(
        "导入缺少模板",
        icon=ft.Icons.UPLOAD_FILE,
        height=36,
        style=compact_button_style,
        disabled=True,
    )
    ai_review_btn = ft.OutlinedButton(
        "审核待审核模板",
        icon=ft.Icons.AUTO_AWESOME,
        height=36,
        style=compact_button_style,
        disabled=True,
    )

    output_dir_field = ft.TextField(label="输出目录", value=cfg.get("output_dir", ""),
                                    read_only=True, expand=True, dense=True)
    # 图片放在 输出目录/日期/店铺/品类/ 下；品类默认取订单表文件名（去掉开头日期）
    category_field = ft.TextField(label="品类文件夹", width=160, dense=True,
                                  tooltip="图片保存到 输出目录/日期/店铺名/品类/；默认取订单表文件名，可修改")
    output_name_column_field = ft.TextField(
        label="图片命名 Excel 列（可选）",
        value=cfg.get("output_name_column", ""),
        hint_text="留空沿用原命名规则",
        width=280,
        dense=True,
        prefix_icon=ft.Icons.DRIVE_FILE_RENAME_OUTLINE,
        tooltip="单模板输出为 列值.png；多模板输出为 列值-1.png、列值-2.png",
    )
    start_generation_btn = ft.FilledButton(
        "预检并生成",
        icon=ft.Icons.PLAY_ARROW,
        width=240,
        height=44,
        style=action_button_style,
        disabled=True,
    )
    order_status_row = ft.Row(spacing=8, wrap=True)

    def refresh_excel_action_state(update: bool = False) -> None:
        templates_ready = bool(state.get("index"))
        order_ready = state.get("excel") is not None
        start_generation_btn.disabled = not (templates_ready and order_ready and bool(output_dir_field.value))
        if update:
            page.update()

    def selected_row_numbers() -> set[int]:
        """Checked ready orders; an empty set means 'all ready orders'."""
        ready = {order.row_no for order in state.get("orders") or [] if order.status == "ready"}
        return set(state.get("selected_rows") or set()) & ready

    def render_rows() -> list:
        """Excel rows in this run's scope: the checked orders, or every row."""
        excel = state.get("excel")
        rows = list(excel.rows) if excel is not None else []
        chosen = selected_row_numbers()
        return [row for row in rows if row.row_no in chosen] if chosen else rows

    def refresh_selection_label() -> None:
        chosen = len(selected_row_numbers())
        start_generation_btn.text = f"预检并生成选中 {chosen} 条" if chosen else "预检并生成"
        clear_selection_btn.visible = bool(chosen)
        clear_selection_btn.text = f"清空勾选（{chosen}）"

    def toggle_row_selection(row_no: int, checked: bool) -> None:
        selected = set(state.get("selected_rows") or set())
        if checked:
            selected.add(row_no)
        else:
            selected.discard(row_no)
        state["selected_rows"] = selected
        refresh_selection_label()
        page.update()

    def select_all_ready(e=None) -> None:
        state["selected_rows"] = {order.row_no for order in state.get("orders") or [] if order.status == "ready"}
        build_order_list()
        page.update()

    def clear_row_selection(e=None) -> None:
        state["selected_rows"] = set()
        build_order_list()
        page.update()

    clear_selection_btn = ft.TextButton("清空勾选", icon=ft.Icons.CLEAR, visible=False,
                                        on_click=clear_row_selection)

    def variant_field_for(sku: str) -> str:
        return settings.get_variant_field(cfg, template_dir_field.value, sku)

    def refresh_order_summary(update: bool = True) -> None:
        """Recompute strict SKU+size matching after Excel/template changes."""
        excel = state.get("excel")
        index = state.get("index") or {}
        if excel is None:
            order_file_status.value = "尚未导入订单 Excel"
            order_summary_text.value = "上传订单后，系统会按 SKU + 严格方向尺寸匹配模板。"
            upload_missing_template_btn.disabled = True
            ai_review_btn.disabled = True
            state["orders"] = []
            state["order_summary"] = None
            return
        orders, summary = order_intake.normalize_orders(excel)
        profile_store = template_profiles.load_store(template_dir_field.value) if template_dir_field.value else {"profiles": {}}
        state["profile_store"] = profile_store
        missing: dict[tuple[str, str], list[int]] = {}
        needs_review: dict[tuple[str, str, str], list[int]] = {}
        order_needs: dict[int, dict] = {}
        analysis_errors = state.get("analysis_errors") or {}
        ready_count = 0
        invalid_count = 0
        for order in orders:
            if order.status == "invalid":
                invalid_count += 1
                continue
            vf = variant_field_for(order.sku)
            variant = order.row.values.get(vf, "") if vf else ""
            matches = (
                templates.pick_by_size(index, order.sku, order.size_keys, variant)
                if order.size_key else templates.pick(index, order.sku, variant)
            )
            if not matches:
                order.status = "missing_template"
                order.status_reason = f"没有匹配模板（{order.size_key or '尺寸未知'}）"
                missing.setdefault((order.sku, order.size_key), []).append(order.row_no)
                continue
            if len(matches) > 1:
                order.status = "needs_review"
                order.status_reason = "同一 SKU+尺寸命中多个模板，需要人工确认：" + template_places(matches)
                needs_review[(order.sku, order.size_key, "multiple")] = (
                    needs_review.get((order.sku, order.size_key, "multiple"), []) + [order.row_no]
                )
                continue
            template = matches[0]
            if text_rules.lacks_text_layers(template, order_pipeline.parse_fields(order.row.custom_info)):
                order.status, order.status_reason = "invalid", text_rules.NO_TEXT_LAYERS
                invalid_count += 1
                continue
            profile = template_profiles.get_profile(
                profile_store, template.sku, template.size_key, template.filename, template.path,
            )
            status, reason = template_profiles.profile_status(profile, template.structure_signature)
            if status == "approved" and not order_analysis.is_rules_profile(profile):
                # Profiles approved before the per-order flow keep working as before.
                order.status, order.status_reason = "ready", reason
            else:
                base = _rules_base(profile, template)
                parsed = order_pipeline.parse_fields(order.row.custom_info)
                _, uncovered = order_analysis.field_coverage(template, parsed, base)
                if base and base.get("status") == "approved" and not uncovered:
                    order.status, order.status_reason = "ready", "复用模板规则，只替换本单数值"
                else:
                    signature = order_analysis.uncovered_signature(uncovered)
                    pending = ((base or {}).get("pending_reviews") or {}).get(signature)
                    error = analysis_errors.get((template.path, order.row_no))
                    order.status = "needs_review"
                    order.status_reason = (
                        error or ("分析结果未通过检查，需人工审核" if pending else
                                  "待分析：" + "、".join(item["label"] for item in uncovered[:3])
                                  + ("…" if len(uncovered) > 3 else "") if uncovered else "待分析")
                    )
                    order_needs[order.row_no] = {
                        "template": template, "uncovered": uncovered, "signature": signature,
                        "pending": bool(pending or error), "error": error or "",
                    }
            if order.status == "ready":
                ready_count += 1
            else:
                needs_review.setdefault((template.sku, template.size_key, "review"), []).append(order.row_no)

        summary["ready_count"] = ready_count
        summary["invalid_count"] = invalid_count
        summary["missing"] = {
            f"{sku} / {size_key}": rows for (sku, size_key), rows in missing.items()
        }
        summary["needs_review"] = {
            f"{sku} / {size_key} / {kind}": rows
            for (sku, size_key, kind), rows in needs_review.items()
        }
        summary["missing_count"] = sum(len(rows) for rows in missing.values())
        summary["needs_review_count"] = sum(len(rows) for rows in needs_review.values())
        state["orders"] = orders
        state["order_summary"] = summary
        state["order_needs"] = order_needs
        order_file_status.value = f"订单：{os.path.basename(state.get('excel_path') or '')} · 共 {len(orders)} 条"
        order_summary_text.value = (
            f"可生成 {ready_count} 条 · 待分析/审核 {summary['needs_review_count']} 条 · "
            f"缺模板 {summary['missing_count']} 条 · 异常 {invalid_count} 条"
        )
        upload_missing_template_btn.disabled = not bool(missing)
        ai_review_btn.disabled = not bool(needs_review)
        build_order_list()
        schedule_auto_analysis()
        if update:
            page.update()

    def _order_status_label(status: str) -> str:
        return {
            "ready": "可生成",
            "needs_review": "待审核",
            "missing_template": "缺模板",
            "invalid": "异常",
        }.get(status, status or "待处理")

    def _order_status_color(status: str) -> str:
        return {
            "ready": ft.Colors.GREEN,
            "needs_review": ft.Colors.AMBER,
            "missing_template": ft.Colors.RED,
            "invalid": ft.Colors.ERROR,
        }.get(status, ft.Colors.ON_SURFACE_VARIANT)

    def set_order_filter(value: str) -> None:
        state["order_filter"] = value
        build_order_list()
        page.update()

    def build_order_list() -> None:
        """Render only task-relevant orders; templates stay on the library page."""
        order_list_view.controls.clear()
        summary = state.get("order_summary") or {}
        orders = state.get("orders") or []
        counts = {
            "all": len(orders),
            "ready": int(summary.get("ready_count") or 0),
            "needs_review": int(summary.get("needs_review_count") or 0),
            "missing_template": int(summary.get("missing_count") or 0),
            "invalid": int(summary.get("invalid_count") or 0),
        }
        order_status_row.controls = [
            ft.FilledTonalButton(
                f"全部 {counts['all']}",
                on_click=lambda e: set_order_filter("all"),
                style=compact_button_style,
                disabled=not orders,
            ),
            ft.OutlinedButton(
                f"可生成 {counts['ready']}",
                on_click=lambda e: set_order_filter("ready"),
                style=compact_button_style,
                disabled=not orders,
            ),
            ft.OutlinedButton(
                f"待审核 {counts['needs_review']}",
                on_click=lambda e: set_order_filter("needs_review"),
                style=compact_button_style,
                disabled=not orders,
            ),
            ft.OutlinedButton(
                f"缺模板 {counts['missing_template']}",
                on_click=lambda e: set_order_filter("missing_template"),
                style=compact_button_style,
                disabled=not orders,
            ),
            ft.OutlinedButton(
                f"异常 {counts['invalid']}",
                on_click=lambda e: set_order_filter("invalid"),
                style=compact_button_style,
                disabled=not orders,
            ),
            ft.TextButton("勾选全部可生成", icon=ft.Icons.CHECK_BOX_OUTLINED, on_click=select_all_ready,
                          disabled=not counts["ready"]),
            clear_selection_btn,
        ]
        refresh_selection_label()
        if not orders:
            order_list_view.controls.append(ft.Container(
                content=ft.Column([
                    ft.Icon(ft.Icons.UPLOAD_FILE, size=32, color=ft.Colors.OUTLINE),
                    ft.Text("导入订单后，这里会显示模板匹配、审核和缺模板处理结果。", size=13),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, tight=True),
                padding=28, alignment=ft.alignment.center,
            ))
            return
        active_filter = state.get("order_filter") or "all"
        visible = [item for item in orders if active_filter == "all" or item.status == active_filter]
        if not visible:
            order_list_view.controls.append(ft.Text("当前筛选下没有订单。", color=ft.Colors.ON_SURFACE_VARIANT))
            return
        for order in visible:
            matched = templates_for_row(order.row) if order.status in {"ready", "needs_review"} else []
            matched_name = matched[0].filename if len(matched) == 1 else "—"
            action = None
            if order.status == "missing_template":
                action = ft.OutlinedButton("导入模板", icon=ft.Icons.UPLOAD_FILE,
                                           style=compact_button_style, on_click=on_upload_missing_template)
            elif order.status == "needs_review":
                target_template = matched[0] if len(matched) == 1 else None
                need = (state.get("order_needs") or {}).get(order.row_no) or {}
                running = {task.get("row_no") for task in (state.get("analysis_tasks") or {}).values()}
                label = ("查看审核" if need.get("pending") else "分析中" if order.row_no in running
                         else "排队中" if need else "查看原因")
                action = ft.OutlinedButton(label, icon=ft.Icons.AUTO_AWESOME,
                                           style=compact_button_style,
                                           on_click=lambda e, t=target_template: show_ai_review_dialog(target_template=t) if t else show_order_summary())
            elif order.status == "ready":
                action = ft.TextButton("查看模板", icon=ft.Icons.LAYERS,
                                       style=text_button_style, on_click=lambda e: show_template_library())
            else:
                action = ft.TextButton("查看原因", style=text_button_style, on_click=show_order_summary)
            if order.status == "ready":
                pick = ft.Checkbox(
                    value=order.row_no in (state.get("selected_rows") or set()),
                    tooltip="勾选后只生成勾选的订单",
                    on_change=lambda e, n=order.row_no: toggle_row_selection(n, bool(e.control.value)),
                )
            else:
                pick = ft.Container(width=40)  # 不可生成的订单不能勾选
            order_list_view.controls.append(ft.Container(
                content=ft.Row([
                    pick,
                    ft.Container(ft.Text(f"#{order.row_no}", weight=ft.FontWeight.BOLD), width=52),
                    ft.Column([
                        ft.Text(order.sku, weight=ft.FontWeight.W_600),
                        ft.Text(f"尺寸：{order.size_key or '无法识别'} · 模板：{matched_name}", size=11,
                                color=ft.Colors.ON_SURFACE_VARIANT),
                    ], expand=True, spacing=2),
                    ft.Column([
                        ft.Text(_order_status_label(order.status), color=_order_status_color(order.status),
                                weight=ft.FontWeight.W_600, size=12),
                        ft.Text(order.status_reason or "", size=10, color=ft.Colors.ON_SURFACE_VARIANT,
                                max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    ], width=250, spacing=2),
                    action,
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=12),
                padding=ft.padding.symmetric(horizontal=12, vertical=10),
                border=ft.border.all(1, ft.Colors.OUTLINE_VARIANT), border_radius=8,
            ))

    def is_aluminum_mode() -> bool:
        summary = state.get("order_summary") or {}
        return bool(summary.get("is_aluminum_order"))

    def normalized_order_for(row):
        return next((item for item in state.get("orders") or [] if item.row_no == row.row_no), None)

    def will_render(row) -> bool:
        """Aluminum mode renders only ready orders; the others are skipped."""
        if not is_aluminum_mode():
            return True
        order = normalized_order_for(row)
        return order is not None and order.status == "ready"

    def templates_for_row(row, field_value: str = "") -> list[templates.Template]:
        if is_aluminum_mode():
            normalized = normalized_order_for(row)
            if not normalized or not normalized.size_key:
                return []
            return templates.pick_by_size(state["index"], row.sku, normalized.size_keys, field_value)
        return templates.pick(state["index"], row.sku, field_value)

    def template_places(items: list[templates.Template]) -> str:
        """Where duplicate templates sit, relative to the library (e.g. 铝板画/x.psd、毛毯/x.psd)."""
        root = template_dir_field.value
        return "、".join(
            os.path.relpath(t.path, root).replace(os.sep, "/") if root else t.filename for t in items
        )

    def aliases_for(sku: str) -> dict:
        return settings.get_layer_aliases(cfg, template_dir_field.value, sku)

    def smart_alias_for(t: templates.Template, smart_path: list[str], text_path: list[str]) -> str:
        return settings.get_smart_object_alias(
            cfg, template_dir_field.value, t.sku, t.filename,
            smart_path, text_path,
        )

    def smart_aliases_for(t: templates.Template) -> list[dict]:
        return settings.get_smart_object_aliases(
            cfg, template_dir_field.value, t.sku, t.filename,
        )

    def smart_configs_for(t: templates.Template) -> list[dict]:
        return settings.get_smart_object_configs(
            cfg, template_dir_field.value, t.sku, t.filename,
        )

    def group_config_for(t: templates.Template) -> dict:
        config = settings.get_group_config(cfg, template_dir_field.value, t.sku, t.filename)
        return {
            "selectors": [
                {
                    "parent": list(item.get("parent") or []),
                    "enabled": bool(item.get("enabled")),
                    "column": str(item.get("column") or ""),
                    "candidates": [dict(candidate) for candidate in item.get("candidates", [])],
                    "relative_text_aliases": dict(item.get("relative_text_aliases", {})),
                }
                for item in config.get("selectors", [])
            ],
        }

    def save_group_config(t: templates.Template, config: dict) -> None:
        settings.set_group_config(cfg, template_dir_field.value, t.sku, t.filename, config)
        settings.save(cfg)

    def smart_config_for(t: templates.Template, smart_path: list[str]) -> dict:
        config = settings.get_smart_object_config(
            cfg, template_dir_field.value, t.sku, t.filename, smart_path,
        )
        return {
            "selectors": [
                {
                    "parent": list(item.get("parent") or []),
                    "enabled": bool(item.get("enabled")),
                    "column": str(item.get("column") or ""),
                    "candidates": [dict(candidate) for candidate in item.get("candidates", [])],
                    "relative_text_aliases": dict(item.get("relative_text_aliases", {})),
                }
                for item in config.get("selectors", [])
            ],
        }

    def save_smart_config(t: templates.Template, smart_path: list[str], config: dict) -> None:
        settings.set_smart_object_config(
            cfg, template_dir_field.value, t.sku, t.filename, smart_path, config,
        )
        settings.save(cfg)

    def image_configs_for(t: templates.Template, enabled_only: bool = False) -> list[dict]:
        entries = settings.get_image_replacement_configs(
            cfg, template_dir_field.value, t.sku, t.filename,
        )
        if enabled_only:
            return [entry for entry in entries if entry.get("enabled")]
        return entries

    def image_config_for(t: templates.Template, layer_path: list[str]) -> dict:
        return settings.get_image_replacement_config(
            cfg, template_dir_field.value, t.sku, t.filename, layer_path,
        )

    def save_image_config(
        t: templates.Template, layer_path: list[str], config: dict | None,
    ) -> None:
        settings.set_image_replacement_config(
            cfg, template_dir_field.value, t.sku, t.filename, layer_path, config,
        )
        settings.save(cfg)

    def build_switch(value: bool, tooltip: str, on_change) -> ft.Control:
        if hasattr(ft, "CupertinoSwitch"):
            return ft.CupertinoSwitch(
                value=value,
                tooltip=tooltip,
                active_color=ft.Colors.PRIMARY,
                track_color=ft.Colors.OUTLINE_VARIANT,
                thumb_color=ft.Colors.SURFACE,
                on_change=on_change,
            )
        return ft.Switch(
            value=value,
            tooltip=tooltip,
            active_color=ft.Colors.ON_PRIMARY,
            active_track_color=ft.Colors.PRIMARY,
            inactive_thumb_color=ft.Colors.SURFACE,
            inactive_track_color=ft.Colors.OUTLINE_VARIANT,
            on_change=on_change,
        )

    def show_image_config_dialog(t: templates.Template, layer_path: list[str]) -> None:
        saved = image_config_for(t, layer_path)
        enabled_switch = ft.Switch(value=bool(saved.get("enabled")), label="启用图片替换")
        column_field = ft.TextField(
            value=str(saved.get("column") or ""), label="Excel 列名", dense=True,
        )
        image_dir_field = ft.TextField(
            value=str(saved.get("image_dir") or ""), label="素材目录",
            read_only=True, dense=True, expand=True,
        )
        search_field = ft.TextField(
            hint_text="搜索图片文件", prefix_icon=ft.Icons.SEARCH,
            dense=True, text_size=13,
        )
        mapping_list = ft.ListView(height=350, spacing=4, padding=4)
        scan_status = ft.Text("", size=11, color=ft.Colors.ON_SURFACE_VARIANT)
        mapping_fields: dict[str, ft.TextField] = {}
        saved_values = {
            str(item.get("file") or "").replace("\\", "/"): str(item.get("value") or "")
            for item in saved.get("mappings") or []
        }
        current_files: set[str] = set()

        def rebuild_mapping_list(update: bool = True) -> None:
            query = str(search_field.value or "").strip().casefold()
            mapping_list.controls.clear()
            filenames = sorted(set(current_files) | set(saved_values), key=str.casefold)
            for filename in filenames:
                if query and query not in filename.casefold():
                    continue
                field = mapping_fields.get(filename)
                if field is None:
                    field = ft.TextField(
                        value=saved_values.get(filename, ""),
                        hint_text="对应 Excel 值", dense=True, width=210, text_size=13,
                    )
                    mapping_fields[filename] = field
                missing = filename not in current_files
                mapping_list.controls.append(ft.Container(
                    content=ft.Row([
                        ft.Icon(
                            ft.Icons.BROKEN_IMAGE_OUTLINED if missing else ft.Icons.IMAGE_OUTLINED,
                            size=16,
                            color=ft.Colors.ERROR if missing else ft.Colors.ON_SURFACE_VARIANT,
                        ),
                        ft.Text(
                            filename, width=330, size=12, selectable=True,
                            color=ft.Colors.ON_ERROR_CONTAINER if missing else None,
                            tooltip=("文件已不在素材目录" if missing else filename),
                            overflow=ft.TextOverflow.ELLIPSIS,
                        ),
                        field,
                    ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    bgcolor=ft.Colors.ERROR_CONTAINER if missing else ft.Colors.SURFACE_CONTAINER_HIGHEST,
                    border_radius=4,
                    padding=ft.padding.symmetric(horizontal=8, vertical=3),
                ))
            scan_status.value = (
                f"扫描到 {len(current_files)} 个图片文件 · "
                f"已配置 {sum(1 for field in mapping_fields.values() if str(field.value or '').strip())} 项"
            )
            if update:
                page.update()

        def scan_directory(show_error: bool = True) -> None:
            current_files.clear()
            directory = str(image_dir_field.value or "").strip()
            if directory:
                try:
                    current_files.update(image_assets.scan_image_files(directory))
                except ValueError as exc:
                    scan_status.value = str(exc)
                    scan_status.color = ft.Colors.ERROR
                    if show_error:
                        alert("素材目录读取失败", str(exc))
                else:
                    scan_status.color = ft.Colors.ON_SURFACE_VARIANT
            rebuild_mapping_list(update=False)

        def choose_image_dir(e) -> None:
            def selected(path: str) -> None:
                state["image_dir_callback"] = None
                image_dir_field.value = path
                scan_directory(show_error=True)
                page.update()

            state["image_dir_callback"] = selected
            open_directory_picker(
                image_dir_picker,
                "选择图片素材目录",
                str(image_dir_field.value or ""),
                selected,
            )

        def save_dialog(e) -> None:
            column = str(column_field.value or "").strip()
            directory = str(image_dir_field.value or "").strip()
            mappings = [
                {"file": filename, "value": str(field.value or "").strip()}
                for filename, field in mapping_fields.items()
                if str(field.value or "").strip()
            ]
            values = [item["value"] for item in mappings]
            duplicates = sorted({value for value in values if values.count(value) > 1})
            if enabled_switch.value and not column:
                alert("无法保存图片替换", "启用图片替换时必须填写 Excel 列名")
                return
            if enabled_switch.value and not os.path.isdir(directory):
                alert("无法保存图片替换", "启用图片替换时必须选择有效的素材目录")
                return
            if duplicates:
                alert("无法保存图片替换", "对应 Excel 值重复: " + "、".join(duplicates))
                return
            save_image_config(t, layer_path, {
                "enabled": bool(enabled_switch.value),
                "column": column,
                "image_dir": directory,
                "mappings": mappings,
            })
            close_dialog(dlg)
            refresh_sku_card(t.sku)

        def delete_config(e) -> None:
            save_image_config(t, layer_path, None)
            close_dialog(dlg)
            refresh_sku_card(t.sku)

        search_field.on_change = lambda e: rebuild_mapping_list()
        scan_directory(show_error=False)
        dlg = ft.AlertDialog(
            title=ft.Text(f"图片替换 · {display_path(layer_path)}"),
            content=ft.Container(
                width=650,
                content=ft.Column([
                    enabled_switch,
                    column_field,
                    ft.Row([
                        image_dir_field,
                        ft.OutlinedButton(
                            "选择目录", icon=ft.Icons.FOLDER_OPEN,
                            style=compact_button_style, on_click=choose_image_dir,
                        ),
                        ft.IconButton(
                            icon=ft.Icons.REFRESH, tooltip="重新扫描素材目录",
                            style=icon_button_style,
                            on_click=lambda e: (scan_directory(show_error=True), page.update()),
                        ),
                    ]),
                    search_field,
                    scan_status,
                    mapping_list,
                ], tight=True, spacing=8),
            ),
            actions=[
                ft.TextButton(
                    "删除配置", icon=ft.Icons.DELETE_OUTLINE,
                    style=text_button_style, on_click=delete_config,
                ),
                ft.TextButton(
                    "取消", style=text_button_style, on_click=lambda e: close_dialog(dlg),
                ),
                ft.FilledButton(
                    "保存", icon=ft.Icons.SAVE, style=compact_button_style,
                    on_click=save_dialog,
                ),
            ],
        )
        page.open(dlg)

    def build_image_config_button(
        t: templates.Template, layer_path: list[str], target_label: str,
    ) -> ft.Control:
        image_config = image_config_for(t, layer_path)
        mapping_count = len(image_config.get("mappings") or [])
        enabled = bool(image_config.get("enabled"))
        return ft.OutlinedButton(
            "图片替换" + (f" · {mapping_count}" if mapping_count else ""),
            icon=ft.Icons.IMAGE_OUTLINED,
            width=150,
            height=34,
            style=compact_button_style,
            tooltip=(
                f"{target_label} · Excel 列: {image_config.get('column') or '未填写'}"
                if enabled else f"配置{target_label}图片替换"
            ),
            on_click=lambda e: show_image_config_dialog(t, layer_path),
        )

    def show_template_config_sync_dialog(
        source: templates.Template, sku_templates: list[templates.Template],
    ) -> None:
        source_configs = image_configs_for(source)
        source_text_entries = template_sync.collect_text_sync_entries(
            source.all_layers, aliases_for(source.sku), group_config_for(source),
            smart_aliases_for(source), smart_configs_for(source),
        )
        target_plans = []
        for target in sku_templates:
            if target.filename == source.filename:
                continue
            image_plan = image_assets.plan_image_config_sync(
                source_configs, source.all_layers,
                target.all_layers, image_configs_for(target),
            )
            text_plan = template_sync.plan_text_config_sync(
                source_text_entries, target.all_layers, aliases_for(target.sku),
                group_config_for(target), smart_aliases_for(target),
                smart_configs_for(target),
            )
            target_plans.append({
                "template": target,
                "image_plan": image_plan,
                "text_plan": text_plan,
                "counts": {
                    status: image_plan["counts"][status] + text_plan["counts"][status]
                    for status in ("add", "overwrite", "unchanged", "ignored")
                },
                "actionable": image_plan["actionable"] + text_plan["actionable"],
            })

        status_meta = {
            "add": ("新增", ft.Colors.SECONDARY, ft.Icons.ADD_CIRCLE_OUTLINE),
            "overwrite": ("覆盖", ft.Colors.TERTIARY, ft.Icons.SYNC),
            "unchanged": ("无需变更", ft.Colors.ON_SURFACE_VARIANT, ft.Icons.CHECK_CIRCLE_OUTLINE),
            "ignored": ("忽略", ft.Colors.ERROR, ft.Icons.REMOVE_CIRCLE_OUTLINE),
        }
        checkboxes: dict[str, ft.Checkbox] = {}
        selection_summary = ft.Text(size=12, color=ft.Colors.ON_SURFACE_VARIANT)
        preview_list = ft.ListView(height=500, spacing=6, padding=4)

        def selected_actionable_count() -> int:
            return sum(
                item["actionable"]
                for item in target_plans
                if bool(checkboxes[item["template"].filename].value)
            )

        def update_selection(e=None) -> None:
            selected = sum(1 for checkbox in checkboxes.values() if checkbox.value)
            actionable = selected_actionable_count()
            selection_summary.value = (
                f"已选择 {selected}/{len(target_plans)} 个目标模板 · "
                f"将写入 {actionable} 项配置"
            )
            confirm_button.disabled = actionable == 0
            page.update()

        for item in target_plans:
            target = item["template"]
            counts = item["counts"]
            checkbox = ft.Checkbox(value=True, on_change=update_selection)
            checkboxes[target.filename] = checkbox
            count_text = (
                f"新增 {counts['add']} · 覆盖 {counts['overwrite']} · "
                f"无需变更 {counts['unchanged']} · 忽略 {counts['ignored']}"
            )
            detail_rows: list[ft.Control] = []
            for action in item["text_plan"]["actions"]:
                label, color, icon = status_meta[action["status"]]
                detail = f"{label} · 文字 · {action['display_path']}"
                if action["reason"]:
                    detail += f" · {action['reason']}"
                detail_rows.append(ft.Row([
                    ft.Icon(icon, size=15, color=color),
                    ft.Text(detail, size=12, color=color, selectable=True, expand=True),
                ], spacing=6))
            for action in item["image_plan"]["actions"]:
                label, color, icon = status_meta[action["status"]]
                detail = f"{label} · 图片 · {display_path(action['layer_path'])}"
                if action["reason"]:
                    detail += f" · {action['reason']}"
                detail_rows.append(ft.Row([
                    ft.Icon(icon, size=15, color=color),
                    ft.Text(detail, size=12, color=color, selectable=True, expand=True),
                ], spacing=6))
            if not detail_rows:
                detail_rows.append(ft.Text(
                    "源模板没有可同步的文字或图片配置",
                    size=12, color=ft.Colors.ON_SURFACE_VARIANT,
                ))
            preview_list.controls.append(ft.Container(
                content=ft.Row([
                    checkbox,
                    ft.ExpansionTile(
                        title=ft.Text(target.filename, size=13, weight=ft.FontWeight.BOLD),
                        subtitle=ft.Text(count_text, size=11, color=ft.Colors.ON_SURFACE_VARIANT),
                        controls=[ft.Column(detail_rows, spacing=5)],
                        controls_padding=ft.padding.only(left=16, right=12, bottom=10),
                        tile_padding=ft.padding.symmetric(horizontal=8),
                        dense=True,
                        initially_expanded=False,
                        expand=True,
                    ),
                ], spacing=2, vertical_alignment=ft.CrossAxisAlignment.START),
                border=ft.border.all(1, ft.Colors.OUTLINE_VARIANT),
                border_radius=6,
            ))

        def apply_sync(e) -> None:
            totals = {"add": 0, "overwrite": 0}
            type_totals = {"text": 0, "image": 0}
            selected_templates = 0
            changed_templates = 0
            for item in target_plans:
                target = item["template"]
                if not checkboxes[target.filename].value:
                    continue
                selected_templates += 1
                template_changed = False
                for action in item["text_plan"]["actions"]:
                    if action["status"] not in {"add", "overwrite"}:
                        continue
                    entry = action["entry"]
                    if entry["scope"] == "group":
                        target_config = group_config_for(target)
                        set_selector_relative_alias(
                            target_config, entry["parent"],
                            entry["relative_path"], entry["column"],
                        )
                        settings.set_group_config(
                            cfg, template_dir_field.value, target.sku,
                            target.filename, target_config,
                        )
                    elif entry["scope"] == "smart":
                        settings.set_smart_object_alias(
                            cfg, template_dir_field.value, target.sku, target.filename,
                            entry["smart_path"], entry["text_path"], entry["column"],
                        )
                    elif entry["scope"] == "smart_group":
                        target_config = smart_config_for(target, entry["smart_path"])
                        set_selector_relative_alias(
                            target_config, entry["parent"],
                            entry["relative_path"], entry["column"],
                        )
                        settings.set_smart_object_config(
                            cfg, template_dir_field.value, target.sku, target.filename,
                            entry["smart_path"], target_config,
                        )
                    totals[action["status"]] += 1
                    type_totals["text"] += 1
                    template_changed = True
                for action in item["image_plan"]["actions"]:
                    if action["status"] not in {"add", "overwrite"}:
                        continue
                    settings.set_image_replacement_config(
                        cfg, template_dir_field.value, target.sku, target.filename,
                        action["layer_path"], action["config"],
                    )
                    totals[action["status"]] += 1
                    type_totals["image"] += 1
                    template_changed = True
                if template_changed:
                    changed_templates += 1
            settings.save(cfg)
            close_dialog(dlg)
            refresh_sku_card(source.sku)
            alert(
                "同步完成",
                f"已检查 {selected_templates} 个模板，实际更新 {changed_templates} 个。"
                f"\n新增 {totals['add']} 项，覆盖 {totals['overwrite']} 项。"
                f"\n文字 {type_totals['text']} 项，图片 {type_totals['image']} 项。",
            )

        confirm_button = ft.FilledButton(
            "确认同步", icon=ft.Icons.SYNC, style=compact_button_style,
            on_click=apply_sync,
        )
        selection_summary.value = (
            f"已选择 {len(target_plans)}/{len(target_plans)} 个目标模板 · "
            f"将写入 {sum(item['actionable'] for item in target_plans)} 项配置"
        )
        confirm_button.disabled = selected_actionable_count() == 0
        dlg = ft.AlertDialog(
            title=ft.Text(f"同步文字与图片配置 · {source.filename}"),
            content=ft.Container(
                width=760,
                content=ft.Column([
                    ft.Text(
                        "按完整图层路径同步。目标模板缺少或不支持的图层会被忽略，"
                        "同路径配置将覆盖，目标模板独有配置会保留。"
                        "普通文字映射在同一 SKU 内本来就会自动共享。",
                        size=12, color=ft.Colors.ON_SURFACE_VARIANT,
                    ),
                    selection_summary,
                    preview_list,
                ], tight=True, spacing=8),
            ),
            actions=[
                ft.TextButton(
                    "取消", style=text_button_style,
                    on_click=lambda e: close_dialog(dlg),
                ),
                confirm_button,
            ],
        )
        page.open(dlg)

    def build_template_sync_button(
        source: templates.Template, sku_templates: list[templates.Template],
    ) -> ft.Control:
        text_config_count = len(template_sync.collect_text_sync_entries(
            source.all_layers, aliases_for(source.sku), group_config_for(source),
            smart_aliases_for(source), smart_configs_for(source),
        ))
        image_config_count = len(image_configs_for(source))
        config_count = text_config_count + image_config_count
        if len(sku_templates) <= 1:
            tooltip = "当前 SKU 只有一个模板"
        elif not config_count:
            tooltip = "请先为当前模板保存至少一项文字或图片配置"
        else:
            tooltip = (
                f"预览并同步文字 {text_config_count} 项、图片 {image_config_count} 项配置"
            )
        return ft.OutlinedButton(
            "同步配置到其他模板",
            icon=ft.Icons.SYNC,
            height=34,
            disabled=len(sku_templates) <= 1 or not config_count,
            tooltip=tooltip,
            style=compact_button_style,
            on_click=lambda e: show_template_config_sync_dialog(source, sku_templates),
        )

    # ---------- SKU 卡片 ----------
    def build_thumb(t: templates.Template, size: int) -> ft.Control:
        if t.thumb and os.path.exists(t.thumb):
            img = ft.Image(src=t.thumb, width=size, height=size,
                           fit=ft.ImageFit.CONTAIN, border_radius=4)
        else:
            img = ft.Container(
                content=ft.Icon(ft.Icons.IMAGE_NOT_SUPPORTED_OUTLINED, color=ft.Colors.ON_SURFACE_VARIANT),
                width=size, height=size, alignment=ft.alignment.center,
                bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST, border_radius=4,
            )
        return ft.Container(content=img, border=ft.border.all(1, ft.Colors.OUTLINE_VARIANT),
                            border_radius=4, tooltip=f"{t.filename}  {t.width}×{t.height}")

    def build_group_config_view(tpls: list[templates.Template], editing: bool) -> ft.Control:
        blocks: list[ft.Control] = []
        for t in tpls:
            config = group_config_for(t)

            if not t.all_layers:
                continue

            if editing:
                def build_smart_object_rows(smart_path: list[str], smart_info: dict,
                                            parent: list[str], depth: int) -> list[ft.Control]:
                    rows: list[ft.Control] = []
                    inner_layers = list(smart_info.get("layers") or [])
                    smart_config = smart_config_for(t, smart_path)

                    if not parent:
                        root_selector = selector_match(smart_config, [])
                        root_enabled = bool(root_selector.get("enabled"))

                        def make_on_smart_root_enabled(t=t, smart_path=smart_path):
                            def handler(e):
                                current = smart_config_for(t, smart_path)
                                now_enabled = bool(selector_match(current, []).get("enabled"))
                                set_selector_enabled(current, [], not now_enabled)
                                save_smart_config(t, smart_path, current)
                                refresh_sku_card(t.sku)
                            return handler

                        def make_on_smart_root_column(t=t, smart_path=smart_path):
                            def handler(e):
                                current = smart_config_for(t, smart_path)
                                set_selector_column(current, [], e.control.value.strip())
                                save_smart_config(t, smart_path, current)
                            return handler

                        root_controls: list[ft.Control] = [
                            ft.Container(width=depth * 20 + 28),
                            build_switch(
                                root_enabled,
                                "用 Excel 列值切换智能对象内的顶层图层组",
                                make_on_smart_root_enabled(),
                            ),
                            ft.Text(
                                "按字段切换智能对象顶层图层",
                                size=11, weight=ft.FontWeight.BOLD, width=230,
                            ),
                            ft.Text(
                                "只在内部顶层图层组之间切换显示",
                                size=11, color=ft.Colors.ON_SURFACE_VARIANT, width=200,
                            ),
                        ]
                        if root_enabled:
                            root_controls.append(ft.TextField(
                                value=root_selector.get("column", ""), dense=True, width=150,
                                hint_text="Excel 列名", text_size=13,
                                on_change=make_on_smart_root_column(),
                            ))
                        rows.append(ft.Container(
                            content=ft.Row(
                                root_controls, spacing=6,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            bgcolor=ft.Colors.TERTIARY_CONTAINER,
                            border_radius=4,
                            padding=ft.padding.symmetric(horizontal=6, vertical=4),
                        ))

                    parent_selector = selector_match(smart_config, parent)
                    parent_enabled = bool(parent_selector.get("enabled"))
                    for inner_layer in direct_child_layer_items(inner_layers, parent):
                        inner_parts = layer_path_parts(inner_layer)
                        inner_name = inner_parts[-1] if inner_parts else ""
                        inner_kind = str(inner_layer.get("kind") or "")
                        inner_visible = bool(inner_layer.get("visible", True))
                        inner_has_children = (
                            inner_kind == "group" and
                            has_child_layer_items(inner_layers, inner_parts)
                        )
                        own_selector = selector_match(smart_config, inner_parts)
                        own_enabled = bool(own_selector.get("enabled"))
                        candidate = candidate_value(parent_selector, inner_parts)
                        inner_key = (
                            f"{t.sku}/{t.filename}/{path_key(smart_path)}"
                            f"/::smart::/{path_key(inner_parts)}"
                        )
                        inner_collapsed = inner_has_children and inner_key not in state["expanded"]

                        def make_on_inner_toggle(sku=t.sku, key=inner_key):
                            def handler(e):
                                if key in state["expanded"]:
                                    state["expanded"].discard(key)
                                else:
                                    state["expanded"].add(key)
                                refresh_sku_card(sku)
                            return handler

                        text_alias = ""
                        text_alias_parent: list[str] | None = None
                        text_alias_rel = ""
                        if inner_kind == "type":
                            if len(inner_parts) > 1:
                                candidate_group = inner_parts[:-1]
                                text_alias_parent = candidate_group[:-1]
                                text_alias_rel = inner_parts[-1]
                                text_selector = selector_match(smart_config, text_alias_parent)
                                text_alias = str(
                                    text_selector.get("relative_text_aliases", {}).get(
                                        text_alias_rel, "",
                                    )
                                ) or smart_alias_for(t, smart_path, inner_parts)
                            else:
                                text_alias = smart_alias_for(t, smart_path, inner_parts)

                        def make_on_smart_alias(t=t, smart_path=smart_path,
                                                text_path=inner_parts,
                                                alias_parent=text_alias_parent,
                                                rel=text_alias_rel):
                            def handler(e):
                                column = e.control.value.strip()
                                if alias_parent is not None:
                                    current = smart_config_for(t, smart_path)
                                    set_selector_relative_alias(current, alias_parent, rel, column)
                                    save_smart_config(t, smart_path, current)
                                else:
                                    settings.set_smart_object_alias(
                                        cfg, template_dir_field.value, t.sku, t.filename,
                                        smart_path, text_path, column,
                                    )
                                    settings.save(cfg)
                            return handler

                        def make_on_smart_candidate(t=t, smart_path=smart_path,
                                                    parent=parent, parts=inner_parts):
                            def handler(e):
                                current = smart_config_for(t, smart_path)
                                set_candidate_value(
                                    current, parent, parts, e.control.value.strip(),
                                )
                                save_smart_config(t, smart_path, current)
                            return handler

                        def make_on_smart_selector_enabled(t=t, smart_path=smart_path,
                                                           parts=inner_parts):
                            def handler(e):
                                current = smart_config_for(t, smart_path)
                                now_enabled = bool(selector_match(current, parts).get("enabled"))
                                set_selector_enabled(current, parts, not now_enabled)
                                save_smart_config(t, smart_path, current)
                                refresh_sku_card(t.sku)
                            return handler

                        def make_on_smart_selector_column(t=t, smart_path=smart_path,
                                                          parts=inner_parts):
                            def handler(e):
                                current = smart_config_for(t, smart_path)
                                set_selector_column(current, parts, e.control.value.strip())
                                save_smart_config(t, smart_path, current)
                            return handler

                        if inner_has_children:
                            inner_toggle: ft.Control = ft.IconButton(
                                icon=(ft.Icons.KEYBOARD_ARROW_RIGHT
                                      if inner_collapsed else ft.Icons.KEYBOARD_ARROW_DOWN),
                                icon_size=16,
                                width=28,
                                height=28,
                                tooltip="展开/折叠智能对象内部图层组",
                                on_click=make_on_inner_toggle(),
                                style=icon_button_style,
                            )
                        else:
                            inner_toggle = ft.Container(width=28, height=28)

                        icon = (
                            ft.Icons.FOLDER_OUTLINED if inner_kind == "group"
                            else ft.Icons.LAYERS_OUTLINED if inner_kind == "smartobject"
                            else ft.Icons.ARTICLE_OUTLINED
                        )
                        icon_color = (
                            ft.Colors.PRIMARY if inner_kind == "group"
                            else ft.Colors.TERTIARY if inner_kind == "smartobject"
                            else ft.Colors.ON_SURFACE_VARIANT
                        )
                        show_candidate = parent_enabled and (bool(parent) or inner_kind == "group")
                        controls: list[ft.Control] = [
                            ft.Container(width=depth * 20),
                            inner_toggle,
                            ft.Icon(icon, size=15, color=icon_color),
                            ft.Text(
                                inner_name, size=12, width=150, selectable=True,
                                tooltip=f"{path_key(smart_path)} › {path_key(inner_parts)}",
                                weight=ft.FontWeight.BOLD if inner_kind == "group" else None,
                            ),
                            ft.Text(
                                layer_kind_status_label(inner_kind, inner_visible),
                                size=11, width=78, color=ft.Colors.ON_SURFACE_VARIANT,
                            ),
                        ]
                        if show_candidate:
                            controls.append(ft.TextField(
                                value=candidate, dense=True, width=110,
                                hint_text="对应列值", text_size=13,
                                on_change=make_on_smart_candidate(),
                            ))
                        else:
                            controls.append(ft.Container(width=110))

                        if inner_has_children:
                            switch_tooltip = "用 Excel 列值切换智能对象内该组的子图层/子组"
                            controls.append(ft.Container(
                                width=126,
                                content=ft.Row([
                                    build_switch(
                                        own_enabled,
                                        switch_tooltip,
                                        make_on_smart_selector_enabled(),
                                    ),
                                    ft.Text(
                                        "切换组内图层", size=11,
                                        color=(ft.Colors.PRIMARY
                                               if own_enabled else ft.Colors.ON_SURFACE_VARIANT),
                                        tooltip=switch_tooltip,
                                    ),
                                ], spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                            ))
                            if own_enabled:
                                controls.append(ft.TextField(
                                    value=own_selector.get("column", ""), dense=True, width=150,
                                    hint_text="Excel 列名", text_size=13,
                                    on_change=make_on_smart_selector_column(),
                                ))
                            else:
                                controls.append(ft.Container(width=150))
                        elif inner_kind == "type":
                            controls.append(ft.Container(width=126))
                            controls.append(ft.TextField(
                                value=text_alias,
                                dense=True, width=150,
                                hint_text="文字列名", text_size=13,
                                tooltip="留空时按内部文字图层名匹配 Excel 列",
                                on_change=make_on_smart_alias(),
                            ))
                        elif inner_kind == "smartobject":
                            controls.append(ft.Container(width=126))
                            reason = str(
                                (inner_layer.get("smart_object") or {}).get("reason") or
                                "暂不支持嵌套智能对象"
                            )
                            controls.append(ft.Text(
                                "不可编辑", size=11, width=150,
                                color=ft.Colors.ON_SURFACE_VARIANT, tooltip=reason,
                            ))
                        else:
                            controls.append(ft.Container(width=126))
                            controls.append(ft.Container(width=150))

                        rows.append(ft.Container(
                            content=ft.Row(
                                controls, spacing=6,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            bgcolor=(ft.Colors.PRIMARY_CONTAINER if inner_kind == "group"
                                     else ft.Colors.TERTIARY_CONTAINER if inner_kind == "smartobject"
                                     else ft.Colors.SURFACE_CONTAINER_HIGHEST),
                            border_radius=4,
                            padding=ft.padding.symmetric(horizontal=6, vertical=2),
                        ))
                        if inner_has_children and not inner_collapsed:
                            rows.extend(build_smart_object_rows(
                                smart_path, smart_info, inner_parts, depth + 1,
                            ))
                    return rows

                def build_tree_rows(parent: list[str], depth: int) -> list[ft.Control]:
                    rows: list[ft.Control] = []
                    parent_selector = selector_match(config, parent)
                    parent_enabled = bool(parent_selector.get("enabled"))
                    for layer in direct_child_layers(t, parent):
                        parts = layer_path_parts(layer)
                        name = parts[-1] if parts else ""
                        kind = str(layer.get("kind") or "")
                        visible = bool(layer.get("visible", True))
                        has_children = has_child_layers(t, parts)
                        smart_info = dict(layer.get("smart_object") or {})
                        smart_layers = list(smart_info.get("layers") or [])
                        smart_has_children = (
                            kind == "smartobject" and
                            bool(smart_info.get("editable")) and
                            bool(smart_layers)
                        )
                        own_selector = selector_match(config, parts)
                        own_enabled = bool(own_selector.get("enabled"))
                        candidate = candidate_value(parent_selector, parts)
                        node_key = f"{t.sku}/{t.filename}/{path_key(parts)}"
                        collapsible_node = (kind == "group" and has_children) or smart_has_children
                        collapsed = collapsible_node and node_key not in state["expanded"]
                        text_alias = ""
                        text_alias_parent: list[str] | None = None
                        text_alias_rel = ""
                        root_text_layer = ""
                        if kind == "type":
                            if len(parts) > 1:
                                candidate_group = parts[:-1]
                                text_alias_parent = candidate_group[:-1]
                                text_alias_rel = parts[-1]
                                text_selector = selector_match(config, text_alias_parent)
                                text_alias = str(
                                    text_selector.get("relative_text_aliases", {}).get(text_alias_rel, "")
                                )
                            else:
                                root_text_layer = parts[0]
                                text_alias = aliases_for(t.sku).get(root_text_layer, "")

                        def make_on_candidate_value(t=t, parent=parent, parts=parts):
                            def handler(e):
                                current = group_config_for(t)
                                set_candidate_value(current, parent, parts, e.control.value.strip())
                                save_group_config(t, current)
                            return handler

                        def make_on_selector_column(t=t, parts=parts):
                            def handler(e):
                                current = group_config_for(t)
                                set_selector_column(current, parts, e.control.value.strip())
                                save_group_config(t, current)
                            return handler

                        def make_on_selector_enabled(t=t, parts=parts):
                            def handler(e):
                                current = group_config_for(t)
                                now_enabled = bool(selector_match(current, parts).get("enabled"))
                                set_selector_enabled(current, parts, not now_enabled)
                                save_group_config(t, current)
                                refresh_sku_card(t.sku)
                            return handler

                        def make_on_text_alias(t=t, parent=text_alias_parent, rel=text_alias_rel):
                            def handler(e):
                                if parent is None:
                                    return
                                current = group_config_for(t)
                                set_selector_relative_alias(
                                    current, parent, rel, e.control.value.strip())
                                save_group_config(t, current)
                            return handler

                        def make_on_root_text_alias(t=t, layer=root_text_layer):
                            def handler(e):
                                if not layer:
                                    return
                                settings.set_layer_alias(
                                    cfg, template_dir_field.value, t.sku,
                                    layer, e.control.value.strip())
                                settings.save(cfg)
                            return handler

                        def make_on_toggle(t=t, sku=t.sku, key=node_key):
                            def handler(e):
                                if key in state["expanded"]:
                                    state["expanded"].discard(key)
                                else:
                                    state["expanded"].add(key)
                                refresh_sku_card(sku)
                            return handler

                        toggle_control: ft.Control
                        if collapsible_node:
                            toggle_control = ft.IconButton(
                                icon=(ft.Icons.KEYBOARD_ARROW_RIGHT
                                      if collapsed else ft.Icons.KEYBOARD_ARROW_DOWN),
                                icon_size=16,
                                width=28,
                                height=28,
                                tooltip=("展开/折叠智能对象内部图层"
                                         if kind == "smartobject" else "展开/折叠"),
                                on_click=make_on_toggle(),
                                style=icon_button_style,
                            )
                        else:
                            toggle_control = ft.Container(width=28, height=28)

                        show_candidate = parent_enabled and (bool(parent) or kind == "group")
                        controls: list[ft.Control] = [
                            ft.Container(width=depth * 20),
                            toggle_control,
                            ft.Icon(
                                (ft.Icons.FOLDER_OUTLINED if kind == "group"
                                 else ft.Icons.LAYERS_OUTLINED if kind == "smartobject"
                                 else ft.Icons.IMAGE_OUTLINED if kind == "pixel"
                                 else ft.Icons.ARTICLE_OUTLINED),
                                size=15,
                                color=(ft.Colors.PRIMARY if kind == "group"
                                       else ft.Colors.TERTIARY if kind == "smartobject"
                                       else ft.Colors.ON_SURFACE_VARIANT),
                            ),
                            ft.Text(name, size=12, width=150, selectable=True, tooltip=path_key(parts),
                                    weight=ft.FontWeight.BOLD if kind == "group" else None),
                            ft.Text(layer_kind_status_label(kind, visible),
                                    size=11, width=78, color=ft.Colors.ON_SURFACE_VARIANT),
                        ]
                        if show_candidate:
                            controls.append(ft.TextField(
                                value=candidate, dense=True, width=110,
                                hint_text="对应列值", text_size=13,
                                on_change=make_on_candidate_value(),
                            ))
                        else:
                            controls.append(ft.Container(width=110))
                        if has_children:
                            switch_label = "切换组内图层"
                            switch_tooltip = "用 Excel 列值切换该组内的子图层/子组"
                            controls.append(ft.Container(
                                width=126,
                                content=ft.Row([
                                    build_switch(
                                        own_enabled,
                                        switch_tooltip,
                                        make_on_selector_enabled()),
                                    ft.Text(
                                        switch_label,
                                        size=11,
                                        color=(ft.Colors.PRIMARY
                                               if own_enabled else ft.Colors.ON_SURFACE_VARIANT),
                                        tooltip=switch_tooltip,
                                    ),
                                ], spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                            ))
                            if own_enabled:
                                controls.append(ft.TextField(
                                    value=own_selector.get("column", ""), dense=True, width=150,
                                    hint_text="Excel 列名", text_size=13,
                                    on_change=make_on_selector_column(),
                                ))
                            else:
                                controls.append(ft.Container(width=150))
                        elif kind == "type" and (text_alias_parent is not None or root_text_layer):
                            controls.append(ft.Container(width=126))
                            controls.append(ft.TextField(
                                value=text_alias, dense=True, width=150,
                                hint_text="文字列名", text_size=13,
                                on_change=(make_on_text_alias()
                                           if text_alias_parent is not None
                                           else make_on_root_text_alias()),
                            ))
                        elif kind == "smartobject":
                            controls.append(ft.Container(width=126))
                            if is_raster_image_smart_object(layer):
                                controls.append(build_image_config_button(
                                    t, parts, "图片智能对象",
                                ))
                            else:
                                if smart_info.get("editable"):
                                    detail = "无内部图层" if not smart_layers else "展开配置内部图层"
                                else:
                                    detail = "不可编辑"
                                controls.append(ft.Text(
                                    detail, size=11, width=150,
                                    color=(ft.Colors.TERTIARY if smart_info.get("editable")
                                           else ft.Colors.ON_SURFACE_VARIANT),
                                    tooltip=str(smart_info.get("reason") or detail),
                                ))
                        elif kind == "pixel":
                            controls.append(ft.Container(width=126))
                            controls.append(build_image_config_button(t, parts, "普通像素层"))
                        else:
                            controls.append(ft.Container(width=126))
                            controls.append(ft.Container(width=150))

                        rows.append(ft.Container(
                            content=ft.Row(
                                controls,
                                spacing=6,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            bgcolor=(ft.Colors.PRIMARY_CONTAINER if kind == "group"
                                     else ft.Colors.TERTIARY_CONTAINER if kind == "smartobject"
                                     else ft.Colors.SURFACE_CONTAINER_HIGHEST),
                            border_radius=4,
                            padding=ft.padding.symmetric(horizontal=6, vertical=2),
                        ))
                        if has_children and not collapsed:
                            rows.extend(build_tree_rows(parts, depth + 1))
                        if smart_has_children and not collapsed:
                            rows.extend(build_smart_object_rows(parts, smart_info, [], depth + 1))
                    return rows

                root_selector = selector_match(config, [])
                root_enabled = bool(root_selector.get("enabled"))

                def make_on_root_enabled(t=t):
                    def handler(e):
                        current = group_config_for(t)
                        now_enabled = bool(selector_match(current, []).get("enabled"))
                        set_selector_enabled(current, [], not now_enabled)
                        save_group_config(t, current)
                        refresh_sku_card(t.sku)
                    return handler

                def make_on_root_column(t=t):
                    def handler(e):
                        current = group_config_for(t)
                        set_selector_column(current, [], e.control.value.strip())
                        save_group_config(t, current)
                    return handler

                root_controls: list[ft.Control] = [
                    build_switch(
                        root_enabled,
                        "用 Excel 列值切换顶层图层组",
                        make_on_root_enabled()),
                    ft.Text("按字段切换顶层图层", size=12, weight=ft.FontWeight.BOLD, width=160),
                    ft.Text("只在顶层图层组之间切换显示", size=11, color=ft.Colors.ON_SURFACE_VARIANT, width=170),
                ]
                if root_enabled:
                    root_controls.append(ft.TextField(
                        value=root_selector.get("column", ""), dense=True, width=160,
                        hint_text="Excel 列名", text_size=13,
                        on_change=make_on_root_column(),
                    ))

                tree_rows = [
                    ft.Container(
                        content=ft.Row(
                            root_controls,
                            spacing=8,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                        border_radius=4,
                        padding=ft.padding.symmetric(horizontal=6, vertical=4),
                    ),
                    *build_tree_rows([], 0),
                ]

                template_key = f"{template_dir_field.value}/{t.sku}/{t.filename}"
                if template_key not in state["seen_template_sections"]:
                    state["seen_template_sections"].add(template_key)
                    if t is tpls[0]:
                        state["expanded_templates"].add(template_key)

                def make_on_template_section_change(key=template_key):
                    def handler(e):
                        if str(e.data).lower() == "true":
                            state["expanded_templates"].add(key)
                        else:
                            state["expanded_templates"].discard(key)
                    return handler

                blocks.append(ft.Container(
                    content=ft.ExpansionTile(
                        title=ft.Row([
                            ft.Text(
                                f"{t.filename}", size=12, weight=ft.FontWeight.BOLD,
                                selectable=True,
                            ),
                            ft.Container(expand=True),
                            build_template_sync_button(t, tpls),
                        ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                        subtitle=ft.Text(
                            f"{len(t.all_layers)} 个图层 · 点击展开或折叠配置",
                            size=11, color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                        controls=[ft.Column([
                            ft.Text(
                                "图层切换规则: 选择 Excel 列名, 再给同一父级下的候选图层填写对应列值",
                                size=11, color=ft.Colors.ON_SURFACE_VARIANT,
                            ),
                            ft.Column(tree_rows, spacing=4),
                        ], spacing=5)],
                        controls_padding=ft.padding.only(left=10, right=10, bottom=10),
                        tile_padding=ft.padding.symmetric(horizontal=10, vertical=2),
                        initially_expanded=template_key in state["expanded_templates"],
                        maintain_state=True,
                        dense=True,
                        on_change=make_on_template_section_change(),
                    ),
                    border=ft.border.all(1, ft.Colors.OUTLINE_VARIANT),
                    border_radius=6,
                ))
            else:
                selector_blocks: list[ft.Control] = []
                for selector in config.get("selectors", []):
                    enabled = bool(selector.get("enabled"))
                    column = str(selector.get("column") or "")
                    candidates = [
                        candidate for candidate in selector.get("candidates", [])
                        if candidate.get("value")
                    ]
                    aliases = selector.get("relative_text_aliases") or {}
                    if not enabled and not aliases:
                        continue
                    chips: list[ft.Control] = []
                    if enabled and column:
                        chips.append(ft.Container(
                            content=ft.Text(
                                f"切换列: {column}",
                                size=12,
                                color=ft.Colors.ON_PRIMARY_CONTAINER,
                            ),
                            bgcolor=ft.Colors.PRIMARY_CONTAINER,
                            border_radius=12,
                            padding=ft.padding.symmetric(horizontal=10, vertical=4),
                        ))
                    if enabled:
                        for candidate in candidates:
                            parts = list(candidate.get("path") or [])
                            chips.append(ft.Container(
                                content=ft.Text(
                                    f"列值 {candidate.get('value')}: {path_key(parts)}",
                                    size=12,
                                    color=ft.Colors.ON_PRIMARY_CONTAINER,
                                ),
                                bgcolor=ft.Colors.PRIMARY_CONTAINER,
                                border_radius=12,
                                padding=ft.padding.symmetric(horizontal=10, vertical=4),
                            ))
                    for rel, column in aliases.items():
                        chips.append(ft.Container(
                            content=ft.Text(
                                f"文字 {rel} ← {column}",
                                size=12,
                                color=ft.Colors.ON_SECONDARY_CONTAINER,
                            ),
                            bgcolor=ft.Colors.SECONDARY_CONTAINER,
                            border_radius=12,
                            padding=ft.padding.symmetric(horizontal=10, vertical=4),
                        ))
                    selector_blocks.append(ft.Column([
                        ft.Text(display_path(list(selector.get("parent") or [])),
                                size=12, weight=ft.FontWeight.BOLD, selectable=True),
                        ft.Row(chips, wrap=True, spacing=6, run_spacing=6),
                    ], spacing=4))

                if selector_blocks:
                    blocks.append(ft.Column([
                        ft.Text(t.filename, size=12, weight=ft.FontWeight.BOLD, selectable=True),
                        ft.Column(selector_blocks, spacing=8),
                    ], spacing=4))
                image_chips = []
                for image_config in image_configs_for(t, enabled_only=True):
                    image_chips.append(ft.Container(
                        content=ft.Text(
                            f"图片 {display_path(image_config['layer_path'])} ← "
                            f"{image_config.get('column') or '未填写'}"
                            f"（{len(image_config.get('mappings') or [])} 项）",
                            size=12,
                            color=ft.Colors.ON_SECONDARY_CONTAINER,
                        ),
                        bgcolor=ft.Colors.SECONDARY_CONTAINER,
                        border_radius=12,
                        padding=ft.padding.symmetric(horizontal=10, vertical=4),
                    ))
                if image_chips:
                    blocks.append(ft.Column([
                        ft.Text(t.filename, size=12, weight=ft.FontWeight.BOLD, selectable=True),
                        ft.Row(image_chips, wrap=True, spacing=6, run_spacing=6),
                    ], spacing=4))

        if not blocks:
            return ft.Text("未配置图层规则", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
        return ft.Column(blocks, spacing=8)

    def build_sku_card(sku: str) -> ft.Control:
        tpls = state["index"][sku]
        multi = len(tpls) > 1
        editing = sku in state["editing"]
        size = THUMB_MULTI if multi else THUMB_SINGLE

        # ---- 左侧: 缩略图 + 文件名; 多模版时底下加「切换模版字段」输入 ----
        thumb_items = [
            ft.Column([
                build_thumb(t, size),
                ft.Text(t.filename, size=11, width=size, max_lines=2,
                        text_align=ft.TextAlign.CENTER, tooltip=t.filename),
            ], spacing=2, horizontal_alignment=ft.CrossAxisAlignment.CENTER)
            for t in tpls
        ]
        left_children: list[ft.Control] = [ft.Row(thumb_items, wrap=True, spacing=8, run_spacing=8)]
        if multi:
            def on_variant_change(e, sku=sku):
                settings.set_variant_field(cfg, template_dir_field.value, sku, e.control.value.strip())
                settings.save(cfg)

            left_children.append(ft.TextField(
                value=variant_field_for(sku), dense=True,
                hint_text="填写切换模版字段", hint_style=ft.TextStyle(size=12),
                text_size=13, on_change=on_variant_change,
            ))
        left_width = max(size + 12, min(len(tpls), 2) * (size + 8) + 8)
        left = ft.Column(left_children, width=left_width, spacing=8)

        # ---- 右侧: SKU 标题 + 图层切换/文字映射配置 ----
        all_layer_count = sum(len(t.all_layers) for t in tpls)

        def toggle_edit(e, sku=sku):
            if sku in state["editing"]:
                state["editing"].discard(sku)
            else:
                state["editing"].add(sku)
            refresh_sku_card(sku)

        def confirm_clear_mappings(e, sku=sku):
            def clear_mappings(e):
                settings.clear_sku_config(cfg, template_dir_field.value, sku)
                settings.save(cfg)
                close_dialog(dlg)
                refresh_sku_card(sku)

            dlg = ft.AlertDialog(
                title=ft.Text("清除映射"),
                content=ft.Text(
                    f"确认清除 SKU「{sku}」的文字映射、图层切换规则和切换模版字段？"
                    "\n图片替换配置也会一并清除。"
                    "\n\n此操作无法撤销。",
                    selectable=True,
                ),
            )
            dlg.actions = [
                ft.TextButton(
                    "取消",
                    height=36,
                    style=text_button_style,
                    on_click=lambda e: close_dialog(dlg),
                ),
                ft.FilledButton(
                    "确认清除",
                    icon=ft.Icons.DELETE_OUTLINE,
                    height=36,
                    style=danger_filled_button_style,
                    on_click=clear_mappings,
                ),
            ]
            page.open(dlg)

        built_in = [
            template_profiles.get_profile(state.get("profile_store") or {}, t.sku, t.size_key, t.filename, t.path)
            for t in tpls
        ]
        approved_count = sum(1 for profile in built_in if profile and profile.get("status") == "approved")

        def confirm_delete_bindings(e, sku=sku):
            approved_templates = [
                t for t in tpls
                if (template_profiles.get_profile(state.get("profile_store") or {}, t.sku, t.size_key, t.filename, t.path) or {}).get("status") == "approved"
            ]
            if not approved_templates:
                alert("没有内置绑定", f"SKU「{sku}」没有已审核的内置模板绑定。")
                return
            def delete_bindings(e):
                store = template_profiles.load_store(template_dir_field.value)
                for template in approved_templates:
                    template_profiles.delete_profile(store, template.sku, template.size_key, template.filename, template.path)
                template_profiles.save_store(template_dir_field.value, store)
                state["profile_store"] = store
                close_dialog(dlg)
                refresh_order_summary()
                build_sku_list()
                alert("已删除内置绑定", "只删除了审核档案和 PSD 绑定；源 PSD/PSB 文件仍保留在模板库中。")
            dlg = ft.AlertDialog(
                title=ft.Text("删除内置模板绑定"),
                content=ft.Text(
                    f"确认删除 SKU「{sku}」的 {len(approved_templates)} 个内置模板绑定？\n\n"
                    "不会删除 PSD/PSB 文件。删除后命中订单会重新进入 AI 草稿审核，不能直接出图。",
                    selectable=True,
                ),
            )
            dlg.actions = [
                ft.TextButton("取消", height=36, style=text_button_style, on_click=lambda e: close_dialog(dlg)),
                ft.FilledButton("删除绑定", icon=ft.Icons.LINK_OFF, height=36,
                                style=danger_filled_button_style, on_click=delete_bindings),
            ]
            page.open(dlg)

        header_controls: list[ft.Control] = [
            ft.Text(sku, size=16, weight=ft.FontWeight.BOLD, selectable=True),
            ft.Text(
                f"{len(tpls)} 个模版 · {all_layer_count} 个图层",
                size=11, color=ft.Colors.ON_SURFACE_VARIANT,
            ),
            ft.Text(
                f"内置绑定 {approved_count}", size=11,
                color=ft.Colors.GREEN if approved_count else ft.Colors.ON_SURFACE_VARIANT,
            ),
            ft.Container(expand=True),
        ]
        if approved_count:
            header_controls.append(ft.TextButton(
                "删除内置绑定",
                icon=ft.Icons.LINK_OFF,
                tooltip="删除审核档案，不删除 PSD/PSB 文件",
                on_click=confirm_delete_bindings,
                height=36,
                style=text_button_style,
            ))
        if editing:
            header_controls.append(ft.TextButton(
                "清除映射",
                icon=ft.Icons.DELETE_OUTLINE,
                tooltip="清除当前 SKU 的全部图层配置和切换模版字段",
                on_click=confirm_clear_mappings,
                height=36,
                style=text_button_style,
            ))
        header_controls.append(ft.TextButton(
            "完成" if editing else "编辑配置",
            on_click=toggle_edit,
            disabled=not all_layer_count,
            height=36,
            style=text_button_style,
        ))
        header = ft.Row(header_controls, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        right = ft.Column([
            header,
            ft.Text("图层切换、文字与图片映射", size=11, color=ft.Colors.ON_SURFACE_VARIANT),
            build_group_config_view(tpls, editing),
        ], expand=True, spacing=6)

        return ft.Container(
            content=ft.Row([left, right], vertical_alignment=ft.CrossAxisAlignment.START,
                           spacing=16),
            border=ft.border.all(1, ft.Colors.OUTLINE_VARIANT), border_radius=8, padding=12,
            data=sku,
        )

    def build_sku_list():
        sku_list_view.controls.clear()
        def category_of(sku: str) -> str:
            return "、".join(sorted({t.category for t in state["index"][sku]}))
        skus = sorted(state["index"], key=lambda sku: (category_of(sku), sku))
        grouped = any(category_of(sku) for sku in skus)
        current = None
        for sku in skus:
            if grouped and category_of(sku) != current:
                current = category_of(sku)
                sku_list_view.controls.append(ft.Text(
                    current or "根目录", size=15, weight=ft.FontWeight.BOLD))
            sku_list_view.controls.append(build_sku_card(sku))
        for key, err in (state["stats"] or {}).get("errors", []):
            sku_list_view.controls.append(
                ft.Text(f"✗ {key} 解析失败: {err}", size=12, color=ft.Colors.ERROR))

    def refresh_sku_card(sku: str):
        for i, ctl in enumerate(sku_list_view.controls):
            if getattr(ctl, "data", None) == sku:
                sku_list_view.controls[i] = build_sku_card(sku)
                break
        page.update()

    # 扫描会写解析缓存和缩略图; 重新扫描与清理检查不能同时跑
    scan_lock = threading.Lock()

    def rescan(force: bool = False):
        d = template_dir_field.value
        if not d:
            return
        if not os.path.isdir(d):
            template_status.value = "目录不存在"
            page.update()
            return
        template_status.value = "扫描中…(首次或缓存失效时需解析 PSD 并生成缩略图, 可能较慢)"
        start_generation_btn.disabled = True
        page.update()

        def work():
            try:
                with scan_lock:
                    index, stats = templates.scan_templates(d, force=force)
                    # 品类子文件夹以前单独当模板库时审核过的档案, 合并到总库
                    imported = template_profiles.merge_nested_stores(d)
            except Exception as e:  # noqa: BLE001
                template_status.value = f"扫描失败: {e}"
                page.update()
                return
            state["index"] = index
            state["stats"] = stats
            template_dir_name_text.value = os.path.basename(os.path.normpath(d)) or d
            state["profile_store"] = template_profiles.load_store(d)
            state["editing"].clear()
            template_status.value = (
                f"已加载: {stats['sku_count']} 个 SKU / {stats['template_count']} 个模版"
                f"（{stats['variant_count']} 个含变体）· 缓存命中 {stats['cache_hits']}/{stats['template_count']}"
            )
            category_counts = stats.get("categories") or {}
            if any(category_counts):
                template_status.value += " · " + "、".join(
                    f"{category or '根目录'} {count}" for category, count in category_counts.items()
                )
            if imported:
                template_status.value += f" · 已合并子文件夹的审核档案 {imported} 条"
            if stats["errors"]:
                template_status.value += f" · {len(stats['errors'])} 个解析失败"
            template_library_path_status.value = f"模板库：{d} · {template_status.value}"
            build_sku_list()
            refresh_order_summary(update=False)
            refresh_excel_action_state()
            page.update()
            install_template_fonts(d)

        threading.Thread(target=work, daemon=True).start()

    def install_template_fonts(directory: str) -> None:
        """Install fonts found in the template library (Photoshop only sees system fonts)."""
        try:
            report = font_installer.install_library_fonts(directory)
        except Exception as exc:  # noqa: BLE001 - font install must never break scanning
            report = {"installed": [], "present": [], "failed": [("字体检查", str(exc))]}
        if report["present"] or report["installed"]:
            template_status.value += f" · 字体 {len(report['present']) + len(report['installed'])} 个已就绪"
        lines: list[str] = []
        if report["installed"]:
            state["fonts_need_ps_restart"] = True
            lines.append("已从模板库自动安装字体：\n" + "\n".join(f"· {name}" for name in report["installed"]))
            lines.append("开始生成时会自动重启 Photoshop，让新字体生效。")
        if report["failed"]:
            lines.append("以下字体未能安装，请手动安装：\n" + "\n".join(f"· {name}：{reason}" for name, reason in report["failed"]))
        if lines:
            alert("模板库字体", "\n\n".join(lines))
        page.update()

    def show_cleanup_dialog() -> None:
        """List records left by deleted templates / unused libraries; delete only after confirmation."""
        d = template_dir_field.value
        if not d or not os.path.isdir(d):
            alert("清理无效记录", "请先选择存在的模板库。")
            return
        if not scan_lock.acquire(blocking=False):
            alert("清理无效记录", "模板库正在扫描，请等扫描完成后再试。")
            return
        previous_status = template_library_path_status.value
        checking = "正在检查可清理的记录…"
        template_library_path_status.value = checking
        page.update()

        def work():
            try:
                # Fresh scan so the list never deletes the profile of a template that is on disk now.
                index, stats = templates.scan_templates(d)
                found = library_cleanup.find_stale(d, index, template_profiles.load_store(d), cfg)
            except Exception as exc:  # noqa: BLE001
                alert("清理无效记录", f"检查失败: {exc}")
                return
            finally:
                scan_lock.release()
                if template_library_path_status.value == checking:
                    template_library_path_status.value = previous_status
            if not stats.get("complete", True):
                # Templates in unreadable folders would look deleted; refuse rather than guess.
                unreadable = "\n".join(f"· {name}：{reason}" for name, reason in stats["errors"][:10])
                alert("清理无效记录", "模板库里有子目录读不到，无法判断哪些模板还在，本次不清理。"
                                      f"\n请检查权限或网络盘后重试。\n\n{unreadable}")
                return
            open_cleanup_dialog(d, found)

        threading.Thread(target=work, daemon=True).start()

    def open_cleanup_dialog(d: str, found: dict) -> None:
        profiles, missing, others = found["profiles"], found["missing_libraries"], found["other_libraries"]
        if not (profiles or missing or others):
            alert("清理无效记录", "没有需要清理的记录。")
            return

        def section(title: str, hint: str, items: list[str], checked: bool) -> tuple[ft.Checkbox, ft.Control]:
            box = ft.Checkbox(label=f"{title}（{len(items)}）", value=checked and bool(items),
                              disabled=not items)
            detail = ft.Text("\n".join(f"· {item}" for item in items[:30])
                             + (f"\n· …另 {len(items) - 30} 项" if len(items) > 30 else ""),
                             size=11, color=ft.Colors.ON_SURFACE_VARIANT, selectable=True, visible=bool(items))
            hint_text = ft.Text(hint, size=11, color=ft.Colors.ON_SURFACE_VARIANT)
            return box, ft.Column([box, hint_text, detail], spacing=2)

        profile_box, profile_view = section(
            "已不存在模板的档案", "模板文件已删除或改名，档案已用不上。", [label for _, label in profiles], True)
        missing_box, missing_view = section(
            "找不到的模板库的界面配置",
            "config.json 里记的目录现在找不到；如果是外接硬盘或网络盘没接上，请不要勾选。默认不勾选。", missing, False)
        other_box, other_view = section(
            "其他模板库的界面配置", "这些模板库还在，以后切回去需要重新配置；默认不勾选。", others, False)

        def do_cleanup(e):
            close_dialog(dlg)
            store = template_profiles.load_store(d)
            removed = library_cleanup.apply(
                d, store, cfg,
                profile_keys=[key for key, _ in profiles] if profile_box.value else [],
                libraries=(missing if missing_box.value else []) + (others if other_box.value else []),
            )
            failures = []
            if removed["profiles"]:
                try:
                    template_profiles.save_store(d, store)
                    state["profile_store"] = store
                    refresh_order_summary()
                except OSError as exc:
                    failures.append(f"模板档案写入失败：{exc}")
                    removed["profiles"] = 0
            if removed["libraries"] and not settings.save(cfg):
                failures.append(f"config.json 写入失败：{settings.CONFIG_PATH}（重启程序后这些配置会恢复）")
                removed["libraries"] = 0
            message = (f"档案 {removed['profiles']} 条、模板库界面配置 {removed['libraries']} 个已清理。"
                       "\n\nPSD/PSB、字体、订单和输出图片均未改动。")
            if failures:
                message += "\n\n" + "\n".join(failures)
            alert("清理失败" if failures else "清理完成", message)

        dlg = ft.AlertDialog(
            title=ft.Text("清理无效记录"),
            content=ft.Container(
                content=ft.Column([
                    ft.Text(f"模板库：{d}\n只删除程序生成的记录，不删除 PSD/PSB、字体、订单和输出图片。此操作无法撤销。",
                            size=12, selectable=True),
                    profile_view, missing_view, other_view,
                ], spacing=12, scroll=ft.ScrollMode.AUTO, tight=True),
                width=560, height=420,
            ),
        )
        dlg.actions = [
            ft.TextButton("取消", height=36, style=text_button_style, on_click=lambda e: close_dialog(dlg)),
            ft.FilledButton("删除勾选项", icon=ft.Icons.DELETE_OUTLINE, height=36,
                            style=danger_filled_button_style, on_click=do_cleanup),
        ]
        page.open(dlg)

    # ---------- 文件选择 ----------
    def set_template_directory(path: str) -> None:
        if path:
            template_dir_field.value = path
            cfg["template_dir"] = path
            settings.save(cfg)
            page.update()
            rescan()

    def on_tpl_dir(e: ft.FilePickerResultEvent):
        set_template_directory(e.path or "")

    def set_output_directory(path: str) -> None:
        if path:
            output_dir_field.value = path
            cfg["output_dir"] = path
            settings.save(cfg)
            refresh_excel_action_state()
            page.update()

    def on_out_dir(e: ft.FilePickerResultEvent):
        set_output_directory(e.path or "")

    def on_output_name_column_change(e) -> None:
        cfg["output_name_column"] = str(e.control.value or "").strip()
        settings.save(cfg)

    def on_image_dir(e: ft.FilePickerResultEvent):
        callback = state.get("image_dir_callback")
        state["image_dir_callback"] = None
        if e.path and callback:
            callback(e.path)

    def on_missing_template_files(e: ft.FilePickerResultEvent):
        if not e.files:
            return
        template_dir = template_dir_field.value
        summary = state.get("order_summary") or {}
        missing_keys = set(summary.get("missing", {}))
        copied: list[str] = []
        errors: list[str] = []
        for selected in e.files:
            source = selected.path
            if not source or os.path.splitext(source)[1].lower() not in {".psd", ".psb"}:
                errors.append(f"不支持的模板文件: {os.path.basename(source or '')}")
                continue
            filename = os.path.basename(source)
            sku, size_key, _, _ = templates.parse_template_filename(filename)
            key = f"{sku} / {size_key}"
            if key not in missing_keys:
                errors.append(f"模板 {filename} 未对应当前缺口: {key}")
                continue
            target_dir = os.path.join(template_dir, "导入模板")
            os.makedirs(target_dir, exist_ok=True)
            target = os.path.join(target_dir, filename)
            try:
                if os.path.abspath(source) != os.path.abspath(target):
                    shutil.copy2(source, target)
                copied.append(filename)
            except OSError as exc:
                errors.append(f"复制 {filename} 失败: {exc}")
        if copied:
            alert("模板已导入", "已复制到模板库的「导入模板」目录:\n" + "\n".join(copied) +
                  ("\n\n模板库支持任意子目录，重新扫描后可继续审核。" if not errors else ""))
            rescan(force=True)
        if errors:
            alert("部分模板未处理", "\n".join(errors))

    def load_excel_path(path: str, remember_dir: bool = True):
        if remember_dir:
            cfg["last_excel_dir"] = os.path.dirname(path)
            settings.save(cfg)
        try:
            # Amazon aluminum-order workbooks carry the production filename in
            # `图片命名`; use it by default instead of the internal fallback.
            data = excel_data.parse_excel(path)
            if not str(output_name_column_field.value or "").strip() and "图片命名" in data.headers:
                output_name_column_field.value = "图片命名"
                cfg["output_name_column"] = "图片命名"
                settings.save(cfg)
                data = excel_data.parse_excel(path, name_column="图片命名")
            elif str(output_name_column_field.value or "").strip():
                data = excel_data.parse_excel(path, name_column=str(output_name_column_field.value).strip())
        except Exception as ex:  # noqa: BLE001
            alert("Excel 解析失败", str(ex))
            return None
        state["excel"] = data
        state["excel_path"] = path
        state["selected_rows"] = set()
        category_field.value = output_layout.category_from_excel(path)
        # Materialize the raw customization dictionary into the same values
        # map consumed by the original selector/text rendering pipeline.
        for row in data.rows:
            order_pipeline.materialize_fields(row)
        refresh_order_summary(update=False)
        return data

    def load_selected_excel(e: ft.FilePickerResultEvent):
        if not e.files:
            return None
        path = e.files[0].path
        cfg["last_excel_dir"] = os.path.dirname(path)
        settings.save(cfg)
        return load_excel_path(path, remember_dir=False)

    def on_select_order_excel(e: ft.FilePickerResultEvent):
        data = load_selected_excel(e)
        if data is None:
            return
        if not data.rows:
            alert("订单没有可处理数据", "Excel 中没有可处理的数据行")
            return
        set_output_directory(excel_output_directory(state["excel_path"]))
        refresh_order_summary()
        refresh_excel_action_state()

    tpl_picker = ft.FilePicker(on_result=on_tpl_dir)
    order_excel_picker = ft.FilePicker(on_result=on_select_order_excel)
    missing_template_picker = ft.FilePicker(on_result=on_missing_template_files)
    out_picker = ft.FilePicker(on_result=on_out_dir)
    image_dir_picker = ft.FilePicker(on_result=on_image_dir)
    page.overlay.extend([
        tpl_picker, order_excel_picker,
        missing_template_picker, out_picker, image_dir_picker,
    ])

    def open_directory_picker(
        picker: ft.FilePicker,
        dialog_title: str,
        initial_directory: str | None,
        on_selected,
    ) -> None:
        initial = existing_directory(initial_directory)
        if sys.platform == "win32":
            try:
                selected = pick_windows_directory(dialog_title, initial)
            except Exception:  # Tk may be unavailable in a custom Python installation.
                picker.get_directory_path(
                    dialog_title=dialog_title,
                    initial_directory=initial,
                )
            else:
                if selected:
                    on_selected(selected)
            return
        picker.get_directory_path(
            dialog_title=dialog_title,
            initial_directory=initial,
        )

    def on_pick_template_directory(e) -> None:
        open_directory_picker(
            tpl_picker,
            "选择模版目录",
            cfg.get("template_dir"),
            set_template_directory,
        )

    def on_pick_output_directory(e) -> None:
        open_directory_picker(
            out_picker,
            "选择输出目录",
            cfg.get("output_dir"),
            set_output_directory,
        )

    def on_pick_order_excel(e):
        order_excel_picker.pick_files(
            dialog_title="选择铝板画订单 Excel",
            allowed_extensions=["xlsx"],
            initial_directory=existing_directory(cfg.get("last_excel_dir")),
        )

    def on_pick_generation_excel(e):
        if not output_dir_field.value:
            alert("无法开始", "请先选择输出目录")
            return
        if state.get("excel") is None:
            alert("无法开始", "请先在订单工作台导入订单 Excel")
            return
        confirm_and_start()

    def on_upload_missing_template(e):
        if not template_dir_field.value:
            alert("无法补充模板", "请先选择模板目录")
            return
        if not (state.get("order_summary") or {}).get("missing"):
            alert("没有缺少模板", "当前订单没有待补充的 SKU+尺寸模板")
            return
        missing_template_picker.pick_files(
            dialog_title="选择缺少的 PSD/PSB 模板",
            allowed_extensions=["psd", "psb"],
            allow_multiple=True,
        )

    def show_ai_settings_dialog(e=None):
        # 只保留一个 API（2026-10-14 用户决定，取消备用 API）；中转站统一用“地址 + Key + 模型名”调用。
        current_url, current_key, current_models = ai_client.environment_config()
        url_field = ft.TextField(
            label="AI 服务地址", value=current_url,
            hint_text="例如 https://relay.example 或 https://relay.example/v1",
            dense=True, width=620,
        )
        key_field = ft.TextField(
            label="API Key", value="", password=True, can_reveal_password=True,
            hint_text="已配置；留空会保留当前 Key", dense=True, width=620,
        )
        key_note = ft.Text(
            "当前 API Key 已配置" if current_key else "尚未配置 API Key",
            size=11, color=ft.Colors.GREEN if current_key else ft.Colors.ERROR,
        )
        fetch_status = ft.Text("填写 URL 和 API Key 后拉取可用模型。", size=11,
                               color=ft.Colors.ON_SURFACE_VARIANT)
        selected_models = list(current_models)
        available_models: list[str] = []
        selected_list = ft.Column(spacing=4)
        available_list = ft.ListView(height=260, spacing=2, padding=4)
        save_btn = ft.FilledButton("保存到 .env", icon=ft.Icons.SAVE, disabled=True,
                                   height=36, style=compact_button_style)
        fetch_btn = ft.FilledTonalButton("拉取模型", icon=ft.Icons.DOWNLOAD,
                                         height=36, style=compact_button_style)

        def render_model_lists() -> None:
            selected_list.controls.clear()
            for index, model in enumerate(selected_models):
                def remove_model(event, value=model):
                    selected_models.remove(value)
                    render_model_lists()
                    page.update()

                def move_model(event, offset, value=model):
                    position = selected_models.index(value)
                    target = position + offset
                    if 0 <= target < len(selected_models):
                        selected_models[position], selected_models[target] = selected_models[target], selected_models[position]
                    render_model_lists()
                    page.update()
                selected_list.controls.append(ft.Row([
                    ft.Text(f"{index + 1}. {model}", expand=True, size=12),
                    ft.IconButton(icon=ft.Icons.ARROW_UPWARD, tooltip="提高优先级", disabled=index == 0,
                                  on_click=lambda e, value=model: move_model(e, -1, value)),
                    ft.IconButton(icon=ft.Icons.ARROW_DOWNWARD, tooltip="降低优先级", disabled=index == len(selected_models) - 1,
                                  on_click=lambda e, value=model: move_model(e, 1, value)),
                    ft.IconButton(icon=ft.Icons.CLOSE, tooltip="移除模型", on_click=remove_model),
                ], spacing=2))
            if not selected_models:
                selected_list.controls.append(
                    ft.Text("还没有选择模型，先拉取再在下面勾选。", size=12, color=ft.Colors.ON_SURFACE_VARIANT))
            available_list.controls.clear()
            for model in available_models:
                def toggle_model(event, value=model):
                    if event.control.value and value not in selected_models:
                        selected_models.append(value)
                    elif not event.control.value and value in selected_models:
                        selected_models.remove(value)
                    render_model_lists()
                    page.update()
                # flet 0.28.2 的 Checkbox 不支持 dense（传了会让拉取后的列表渲染报错）
                available_list.controls.append(ft.Checkbox(label=model, value=model in selected_models, on_change=toggle_model))
            if not available_models:
                available_list.controls.append(
                    ft.Text("点“拉取模型”获取中转站可用模型。", size=12, color=ft.Colors.ON_SURFACE_VARIANT))
            save_btn.disabled = not bool(selected_models)

        render_model_lists()
        content = ft.Column([
            ft.Text("连接信息将保存到项目根目录 .env；API Key 不会写入用户配置或模板档案。", size=12),
            url_field,
            key_field,
            key_note,
            ft.Row([fetch_btn, fetch_status], vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Divider(height=1),
            ft.Text("已选模型（顺序即回退顺序：第 1 个失败时依次换下一个）", size=12, weight=ft.FontWeight.BOLD),
            ft.Container(content=selected_list, padding=8, border=ft.border.all(1, ft.Colors.OUTLINE_VARIANT), border_radius=8),
            ft.Text("中转站可选模型（勾选加入）", size=12, weight=ft.FontWeight.BOLD),
            ft.Container(content=available_list, border=ft.border.all(1, ft.Colors.OUTLINE_VARIANT), border_radius=8),
        ], spacing=8, scroll=ft.ScrollMode.AUTO)
        dlg = ft.AlertDialog(title=ft.Text("AI 设置"), content=ft.Container(content=content, width=690, height=650))

        def fetch(e=None, quiet: bool = False):
            """Pull the model list in the background; ``quiet`` = auto-pull on open."""
            try:
                url = ai_client.validate_base_url(url_field.value)
                key = ai_client.validate_api_key(key_field.value or current_key)
            except ai_client.AIClientError as exc:
                fetch_status.value = str(exc)
                fetch_status.color = ft.Colors.ERROR
                page.update()
                return
            fetch_btn.disabled = True
            fetch_status.value = "正在拉取模型…"
            fetch_status.color = ft.Colors.ON_SURFACE_VARIANT
            if not quiet:
                page.update()

            def work():
                try:
                    fetched = ai_client.fetch_models(url, key)
                    dropped = [model for model in selected_models if model not in fetched]
                    available_models[:] = fetched
                    selected_models[:] = [model for model in selected_models if model in fetched]
                    note = f"已拉取 {len(fetched)} 个模型，请勾选并排序。"
                    if dropped:
                        note += f"中转站没有、已移出已选：{', '.join(dropped)}"
                    fetch_status.value = note
                    fetch_status.color = ft.Colors.GREEN
                except Exception as exc:  # noqa: BLE001
                    fetch_status.value = str(exc)
                    fetch_status.color = ft.Colors.ERROR
                fetch_btn.disabled = False
                render_model_lists()
                page.update()
            threading.Thread(target=work, daemon=True).start()

        def save(e):
            try:
                url = ai_client.validate_base_url(url_field.value)
                key = ai_client.validate_api_key(key_field.value or current_key)
                ai_client.save_environment_config(url, key, selected_models)
            except ai_client.AIClientError as exc:
                fetch_status.value = str(exc)
                fetch_status.color = ft.Colors.ERROR
                page.update()
                return
            if cfg.pop("ai_profiles", None) is not None:
                settings.save(cfg)  # 旧的备用 API 配置（不含 Key）一并清掉
            key_field.value = ""
            # Re-saving after a top-up / key change lifts the quota block.
            ai_runtime.reset_blocks()
            state["ai_alert_shown"] = False
            close_dialog(dlg)
            alert("AI 设置已保存", "已写入项目根目录 .env，下次启动自动读取，本次运行中立即生效。")

        fetch_btn.on_click = fetch
        save_btn.on_click = save
        dlg.actions = [
            ft.TextButton("取消", height=36, style=text_button_style, on_click=lambda e: close_dialog(dlg)),
            save_btn,
        ]
        page.open(dlg)
        # 已配置过地址和 Key 时打开就自动拉取，直接勾选即可
        if current_url and current_key:
            fetch(quiet=True)

    def show_order_summary(e=None):
        summary = state.get("order_summary") or {}
        if not summary:
            alert("订单工作台", "请先导入订单 Excel")
            return
        lines = [order_summary_text.value]
        if summary.get("missing"):
            lines.append("\n缺少模板：")
            lines.extend(f"· {key}：第 {', '.join(map(str, rows))} 行" for key, rows in summary["missing"].items())
        if summary.get("needs_review"):
            lines.append("\n待审核模板：")
            lines.extend(f"· {key}：第 {', '.join(map(str, rows))} 行" for key, rows in summary["needs_review"].items())
        alert("订单匹配结果", "\n".join(lines))

    # ---------- 按订单分析（2026-10-13）----------
    # 每条订单单独分析：模板规则已覆盖的订单直接复用（只换数值）；有新字段的
    # 订单把这一单的完整定制文案交给 AI，程序只做检查，通过即自动启用，
    # 不通过才进入人工审核。一次只分析一条，分析完再看下一条是否已被覆盖。
    def _task_key(template: templates.Template) -> str:
        return template_profiles.profile_key(template.sku, template.size_key, template.filename, template.path)

    def _profile_for(template: templates.Template, store: dict | None = None) -> dict | None:
        store = store if store is not None else (state.get("profile_store") or template_profiles.load_store(template_dir_field.value))
        return template_profiles.get_profile(store, template.sku, template.size_key, template.filename, template.path)

    def _rules_base(profile: dict | None, template: templates.Template) -> dict | None:
        """Rules an order builds on: same PSD structure and approved or per-order rules."""
        if not profile or str(profile.get("structure_signature") or "") != template.structure_signature:
            return None
        if profile.get("status") == "approved" or order_analysis.is_rules_profile(profile):
            return profile
        return None

    # AI 出任何问题（Key 无效、额度用完、模型不可用、连不上、返回内容不对）都弹窗请用户更换 API
    # （2026-10-14 用户需求）。同一次运行只弹一次，在「AI 设置」重新保存后恢复提醒。
    _AI_PROBLEM_REASONS = {
        "quota_exhausted": "额度已用完",
        "auth": "API Key 无效或没有权限",
        "model_unavailable": "所选模型在中转站不可用",
        "rate_limit": "请求太频繁，被中转站限流",
        "timeout": "连接 AI 服务超时",
        "transient_network": "连不上 AI 服务（网络或中转站故障）",
        "transient_http": "中转站暂时故障",
        "deadline": "AI 分析超过总时间限制",
        "invalid_response": "AI 返回的内容不符合要求（可能该模型不兼容）",
        "service_error": "中转站返回错误",
        "invalid_request": "中转站拒绝了请求（可能该模型不兼容）",
        "invalid_config": "AI 配置不完整",
    }

    def _show_ai_problem_alert(exc: Exception) -> None:
        if state.get("ai_alert_shown"):
            return
        state["ai_alert_shown"] = True
        reason = _AI_PROBLEM_REASONS.get(str(getattr(exc, "kind", "") or ""), "AI 调用失败")
        dlg = ft.AlertDialog(
            title=ft.Text("AI 的 API 出现问题"),
            content=ft.Text(
                f"AI 的 API 出现问题，请更换 AI 的 API。\n\n原因：{reason}\n详情：{exc}\n\n"
                "本次已改用本地规则；规则确定不了的订单会进入人工审核。\n"
                "在「AI 设置」里更换地址、Key 或模型并保存后即可恢复。",
                selectable=True,
            ),
        )

        def open_settings(e):
            close_dialog(dlg)
            show_ai_settings_dialog()
        dlg.actions = [
            ft.TextButton("稍后", height=36, style=text_button_style, on_click=lambda e: close_dialog(dlg)),
            ft.FilledButton("打开 AI 设置", height=36, style=compact_button_style, on_click=open_settings),
        ]
        page.open(dlg)
        page.update()

    def schedule_auto_analysis() -> None:
        """Start the next order analysis (one at a time, each order isolated)."""
        if not state.get("excel") or not template_dir_field.value:
            return
        with state["analysis_lock"]:
            if state["analysis_tasks"]:
                return
            waiting = [(row_no, need) for row_no, need in sorted((state.get("order_needs") or {}).items())
                       if not need.get("pending")]
            if not waiting:
                return
            row_no, need = waiting[0]
            template = need["template"]
            key = _task_key(template)
            task_id = uuid.uuid4().hex
            state["analysis_tasks"][key] = {
                "task_id": task_id, "row_no": row_no,
                "template_dir": os.path.abspath(template_dir_field.value),
                "started_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            }
        order = next((item for item in state.get("orders") or [] if item.row_no == row_no), None)
        if order is None:
            with state["analysis_lock"]:
                state["analysis_tasks"].pop(key, None)
            return
        threading.Thread(target=_analyze_order, args=(order, template, key, task_id), daemon=True).start()

    def _analyze_order(order, template: templates.Template, key: str, task_id: str) -> None:
        directory = template_dir_field.value
        notes: list[str] = []
        model = "local"
        try:
            base = _rules_base(_profile_for(template, template_profiles.load_store(directory)), template)
            parsed = order_pipeline.parse_fields(order.row.custom_info)
            covered, uncovered = order_analysis.field_coverage(template, parsed, base)
            signature = order_analysis.uncovered_signature(uncovered)
            suggestion = None
            ai_errors: list[str] = []
            providers = ai_client.provider_configs(settings_cfg=cfg)
            if uncovered and providers:
                try:
                    result = ai_client.analyze_order(
                        providers=providers, sku=template.sku, size_key=template.size_key,
                        filename=template.filename,
                        request=order_analysis.ai_request(template, parsed, covered, uncovered),
                    )
                    suggestion, ai_errors = ai_mapping.validate_mapping_suggestion(result.payload, template)
                    model = f"{result.provider_name} / {result.model}" if result.provider_name else result.model
                except ai_client.AIClientError as exc:
                    _show_ai_problem_alert(exc)
                    notes.append(f"AI 不可用（{exc}），已改用本地规则")
            elif uncovered:
                notes.append("未配置 AI，已使用本地规则")
            if suggestion is None:
                suggestion = order_analysis.local_suggestion(template, uncovered)
            candidate = order_analysis.merge_rules(template, base, suggestion, uncovered)
            candidate.pop("pending_reviews", None)
            candidate["template_binding"] = template_profiles.bind_template(template, directory)
            problems = order_analysis.check_candidate(template, order.row, cfg, directory, candidate)

            store = template_profiles.load_store(directory)
            current = _rules_base(_profile_for(template, store), template)
            pending = dict((current or {}).get("pending_reviews") or {})
            if not problems:
                approved = template_profiles.approve_profile(candidate, template.structure_signature, model)
                pending.pop(signature, None)
                approved["pending_reviews"] = pending
                approved["analysis"] = template_profiles.make_analysis(
                    state="ready", model=model,
                    message=f"第 {order.row_no} 行订单分析通过检查，已自动启用" + ("；" + "；".join(notes) if notes else ""),
                )
                template_profiles.put_profile(store, approved)
            else:
                target = copy.deepcopy(current) if current else order_analysis.merge_rules(template, None, {})
                target["template_binding"] = template_profiles.bind_template(template, directory)
                pending[signature] = {
                    "row_no": order.row_no, "sku": order.sku,
                    "excel": os.path.basename(state.get("excel_path") or ""),
                    "custom_info": order.row.custom_info, "fields": uncovered,
                    "candidate": candidate, "problems": problems, "ai_errors": ai_errors,
                    "model": model, "message": "；".join(notes),
                    "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                }
                target["pending_reviews"] = pending
                if target.get("status") != "approved":
                    target["analysis"] = template_profiles.make_analysis(
                        state="ready", model=model, message="分析结果未通过检查，等待人工审核",
                    )
                template_profiles.put_profile(store, target)
            if os.path.abspath(directory) == os.path.abspath(template_dir_field.value):
                template_profiles.save_store(directory, store)
                state["profile_store"] = store
        except Exception as exc:  # noqa: BLE001 - one bad order must not stop the queue
            # Remembered across refreshes so a deterministic failure is not retried in a loop.
            state.setdefault("analysis_errors", {})[(template.path, order.row_no)] = f"分析出错：{exc}"
        finally:
            with state["analysis_lock"]:
                current_task = state["analysis_tasks"].get(key)
                if current_task and current_task.get("task_id") == task_id:
                    state["analysis_tasks"].pop(key, None)
            try:
                refresh_order_summary()
            except Exception:  # noqa: BLE001
                pass

    def show_ai_review_dialog(e=None, target_template: templates.Template | None = None):
        needs = state.get("order_needs") or {}
        items = [(row_no, need) for row_no, need in sorted(needs.items())
                 if target_template is None or need["template"].path == target_template.path]
        if not items:
            alert("订单审核", "当前没有需要审核的订单")
            return
        store = template_profiles.load_store(template_dir_field.value)
        cards: list[ft.Control] = []
        rows_by_pending: dict[tuple[str, str], list[int]] = {}
        for row_no, need in items:
            rows_by_pending.setdefault((need["template"].path, need["signature"]), []).append(row_no)
        shown: set[tuple[str, str]] = set()
        waiting = False
        for row_no, need in items:
            template = need["template"]
            marker = (template.path, need["signature"])
            if marker in shown:
                continue
            shown.add(marker)
            rows = rows_by_pending[marker]
            title = ft.Text(f"第 {'、'.join(map(str, rows))} 行 · {template.filename}", weight=ft.FontWeight.BOLD, size=13)
            profile = _rules_base(_profile_for(template, store), template)
            pending = ((profile or {}).get("pending_reviews") or {}).get(need["signature"])
            if need.get("error"):
                cards.append(ft.Container(content=ft.Column([
                    title, ft.Text(need["error"], color=ft.Colors.ERROR, size=12),
                ], spacing=6), padding=12, border=ft.border.all(1, ft.Colors.ERROR), border_radius=8))
                continue
            if not pending:
                waiting = True
                active = _task_key(template) in state.get("analysis_tasks", {})
                cards.append(ft.Container(content=ft.Column([
                    title,
                    ft.Row([ft.ProgressRing(width=16, height=16) if active else ft.Icon(ft.Icons.SCHEDULE, size=16),
                            ft.Text("正在分析这一单…" if active else "排队等待分析（一次分析一单）", size=12)], spacing=8),
                    ft.Text("待处理字段：" + "、".join(f"{item['label']}={item['value']}" for item in need["uncovered"]) or "无",
                            size=11, color=ft.Colors.ON_SURFACE_VARIANT),
                ], spacing=6), padding=12, border=ft.border.all(1, ft.Colors.OUTLINE_VARIANT), border_radius=8))
                continue
            cards.append(_pending_card(template, need["signature"], pending, title))

        content = ft.ListView(controls=cards, spacing=10, padding=4)
        dlg = ft.AlertDialog(title=ft.Text(f"订单审核（{len(shown)} 项）"),
                             content=ft.Container(content=content, width=880, height=620))
        state["review_dialog"] = dlg
        dlg.actions = [ft.TextButton("关闭", height=36, style=text_button_style, on_click=lambda e: close_dialog(dlg))]
        page.open(dlg)

        async def refresh_while_waiting():
            # Only poll while some order is still queued/analyzing; reopen once
            # when that changes so manual edits are never discarded mid-typing.
            before = {row_no: bool(need.get("pending")) for row_no, need in items}
            for _ in range(150):
                await asyncio.sleep(2)
                if not getattr(dlg, "open", False):
                    return
                now = state.get("order_needs") or {}
                after = {row_no: bool((now.get(row_no) or {}).get("pending")) for row_no, _ in items}
                if after != before:
                    close_dialog(dlg)
                    show_ai_review_dialog(target_template=target_template)
                    return

        if waiting:
            page.run_task(refresh_while_waiting)

    def _pending_card(template: templates.Template, signature: str, pending: dict, title: ft.Control) -> ft.Control:
        candidate = dict(pending.get("candidate") or {})
        fields = list(pending.get("fields") or [])

        def norm(value) -> str:
            return " ".join(str(value or "").split()).casefold()

        # Fields AI already ignored or turned into layer switches are not text.
        settled = order_analysis._ignored_labels(candidate) | {  # noqa: SLF001 - same parsing on purpose
            norm(item.get("source_key")) for item in candidate.get("selector_mappings") or []}
        text_items = [item for item in fields
                      if text_rules.classify_field(item["label"])
                      or (item.get("kind") not in ("material", *text_rules.STYLE_LABELS)
                          and norm(item["label"]) not in settled)]
        color_items = [item for item in fields
                       if item.get("kind") == text_rules.STYLE_COLOR and not text_rules.color_hex(item["value"])]
        known_colors = {norm(name): value for name, value in (candidate.get("color_values") or {}).items()}
        mapped = {norm(item.get("source_key")): item for item in candidate.get("text_mappings") or []}
        text_rows: list[tuple[dict, ft.TextField]] = []
        color_rows: list[tuple[dict, ft.TextField]] = []
        controls: list[ft.Control] = [title]
        if pending.get("message"):
            controls.append(ft.Text(pending["message"], size=11, color=ft.Colors.ON_SURFACE_VARIANT))
        controls.append(ft.Container(
            content=ft.Text("未通过的检查：\n" + "\n".join(f"· {item}" for item in pending.get("problems") or []),
                            size=11, selectable=True, color=ft.Colors.ON_ERROR_CONTAINER),
            padding=8, bgcolor=ft.Colors.ERROR_CONTAINER, border_radius=6,
        ))
        if pending.get("ai_errors"):
            controls.append(ft.Text("AI 建议中被丢弃的项：\n" + "\n".join(f"· {item}" for item in pending["ai_errors"][:8]),
                                    size=10, selectable=True, color=ft.Colors.ON_SURFACE_VARIANT))
        for item in text_items:
            mapping = mapped.get(" ".join(item["label"].split()).casefold())
            text_field = ft.TextField(value=text_rules.layer_label(mapping) if mapping else "", dense=True,
                                      label=f"{item['label']}（{item['value']}）写入的文字图层，留空 = 不替换")
            text_rows.append((item, text_field))
            controls.append(text_field)
        if text_items:
            layer_lines = "\n".join(
                f"· {text_rules.layer_label(layer)}（{'年份' if layer['role'] == text_rules.YEAR else '姓名'}）"
                for layer in text_rules.text_layers(template)
            ) or "模板没有可见文字图层"
            controls.append(ft.Text("可选文字图层：\n" + layer_lines, size=11, selectable=True,
                                    color=ft.Colors.ON_SURFACE_VARIANT))
        for item in color_items:
            color_field = ft.TextField(value=known_colors.get(norm(item["value"]), ""), dense=True,
                                       label=f"{item['label']}（{item['value']}）的色值，如 #FFB6C1，留空 = 不改颜色")
            color_rows.append((item, color_field))
            controls.append(color_field)
        shown = {id(item) for item in (*text_items, *color_items)}
        other = [item for item in fields if id(item) not in shown]
        if other:
            controls.append(ft.Text(
                "其他待处理字段（审核通过后，没有图层规则的将不参与出图）：" +
                "、".join(f"{item['label']}={item['value']}" for item in other), size=11))

        def approve(e):
            final = copy.deepcopy(candidate)
            texts = [item for item in final.get("text_mappings") or []
                     if " ".join(str(item.get("source_key") or "").split()).casefold()
                     not in {" ".join(row["label"].split()).casefold() for row, _ in text_rows}]
            ignored = list(final.get("ignored_fields") or [])
            for item, text_field in text_rows:
                smart_path, target_path = text_rules.parse_layer_label(text_field.value)
                if not target_path:
                    ignored.append(item["label"])
                    continue
                texts.append({
                    "section": item["section"], "source_key": item["label"],
                    "field_key": order_pipeline.serialize_field_key(item["section"], item["label"]),
                    "kind": text_rules.classify_field(item["label"]) or text_rules.TEXT,
                    "smart_object_path": smart_path, "target_path": target_path,
                    "confidence": 1.0, "source": "review",
                })
            final["text_mappings"] = texts
            colors = dict(final.get("color_values") or {})
            for item, color_field in color_rows:
                hex_value = text_rules.normalize_hex(color_field.value)
                if not str(color_field.value or "").strip():
                    ignored.append(item["label"])
                elif not hex_value:
                    alert("色值格式不对", f"「{item['label']}」的色值请写成 #RRGGBB，例如 #FFB6C1")
                    return
                else:
                    colors[item["value"]] = hex_value
            final["color_values"] = colors
            row = excel_data.ExcelRow(int(pending.get("row_no") or 0), str(pending.get("sku") or template.sku),
                                      custom_info=str(pending.get("custom_info") or ""))
            parsed = order_pipeline.parse_fields(row.custom_info)
            _, still = order_analysis.field_coverage(template, parsed, {**final, "ignored_fields": ignored})
            ignored.extend(item["label"] for item in still
                           if not text_rules.classify_field(item["label"]) and item["kind"] not in text_rules.STYLE_LABELS)
            final["ignored_fields"] = list(dict.fromkeys(ignored))
            problems = order_analysis.check_candidate(template, row, cfg, template_dir_field.value, final)
            if problems:
                alert("仍未通过检查", "以下问题需要修改后才能通过（素材类问题需修改 PSD 图层命名或联系开发）：\n" +
                      "\n".join(f"· {item}" for item in problems))
                return
            store = template_profiles.load_store(template_dir_field.value)
            current = _rules_base(_profile_for(template, store), template)
            remaining = dict((current or {}).get("pending_reviews") or {})
            remaining.pop(signature, None)
            approved = template_profiles.approve_profile(final, template.structure_signature, "review")
            approved["pending_reviews"] = remaining
            approved["template_binding"] = template_profiles.bind_template(template, template_dir_field.value)
            template_profiles.put_profile(store, approved)
            template_profiles.save_store(template_dir_field.value, store)
            state["profile_store"] = store
            dlg = state.get("review_dialog")
            if dlg is not None:
                close_dialog(dlg)
            refresh_order_summary()
            alert("审核通过", f"{template.filename} 的规则已更新，同类订单以后直接复用。")

        controls.append(ft.Row([
            ft.FilledButton("审核通过", icon=ft.Icons.CHECK, height=34, style=compact_button_style, on_click=approve),
            ft.TextButton("重新分析", icon=ft.Icons.REFRESH, height=34, style=text_button_style,
                          on_click=lambda e: (close_dialog(state["review_dialog"]), rerun_template_analysis(template, signature))),
        ], spacing=8))
        return ft.Container(content=ft.Column(controls, spacing=7), padding=12,
                            border=ft.border.all(1, ft.Colors.PRIMARY_CONTAINER), border_radius=8)

    def rerun_template_analysis(template: templates.Template, signature: str = "") -> None:
        """Drop the stored review result(s) so the order(s) are analyzed again."""
        state["analysis_errors"] = {
            key: value for key, value in (state.get("analysis_errors") or {}).items() if key[0] != template.path
        }
        store = template_profiles.load_store(template_dir_field.value)
        profile = _profile_for(template, store)
        if profile:
            pending = dict(profile.get("pending_reviews") or {})
            if signature:
                pending.pop(signature, None)
            else:
                pending.clear()
            if profile.get("status") != "approved" and not pending:
                template_profiles.delete_profile(store, template.sku, template.size_key, template.filename, template.path)
            else:
                profile["pending_reviews"] = pending
                template_profiles.put_profile(store, profile)
            template_profiles.save_store(template_dir_field.value, store)
            state["profile_store"] = store
        refresh_order_summary()

    start_generation_btn.on_click = on_pick_generation_excel
    upload_missing_template_btn.on_click = on_upload_missing_template
    ai_review_btn.on_click = show_ai_review_dialog
    output_name_column_field.on_change = on_output_name_column_change

    header_bar = ft.Row([
        ft.Row([
            ft.Text("PSD 批量生图", size=22, weight=ft.FontWeight.BOLD),
            ft.Text(VERSION, size=12, color=ft.Colors.ON_SURFACE_VARIANT),
        ], tight=True),
        ft.Row([
            ft.TextButton("模板库管理", icon=ft.Icons.LAYERS, on_click=lambda e: show_template_library()),
            ft.TextButton("AI 设置", icon=ft.Icons.AUTO_AWESOME, on_click=show_ai_settings_dialog),
        ], tight=True),
    ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)

    config_view = ft.Column([
        header_bar,
        ft.Container(
            content=ft.Row([
                ft.Column([
                    ft.Text("当前模板库", size=11, color=ft.Colors.ON_SURFACE_VARIANT),
                    template_dir_name_text,
                    template_status,
                ], expand=True, spacing=2),
                ft.OutlinedButton("切换模板库", icon=ft.Icons.FOLDER_OPEN, height=36,
                                  style=compact_button_style, on_click=on_pick_template_directory),
                ft.IconButton(icon=ft.Icons.REFRESH, tooltip="重新扫描模板库",
                              on_click=lambda e: rescan(force=True)),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=12, border=ft.border.all(1, ft.Colors.OUTLINE_VARIANT), border_radius=10,
        ),
        ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Column([
                        ft.Text("订单工作台", size=17, weight=ft.FontWeight.BOLD),
                        order_file_status,
                        order_summary_text,
                    ], expand=True, spacing=3),
                    ft.FilledButton("导入订单 Excel", icon=ft.Icons.UPLOAD_FILE, height=42,
                                    style=compact_button_style, on_click=on_pick_order_excel),
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                order_status_row,
                ft.Row([upload_missing_template_btn, ai_review_btn], spacing=8),
            ], spacing=10),
            padding=14, border=ft.border.all(1, ft.Colors.OUTLINE_VARIANT), border_radius=10,
        ),
        ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Text("订单明细", size=15, weight=ft.FontWeight.BOLD),
                    ft.Text("勾选可生成订单只生成勾选的；不勾选则生成全部可生成订单", size=11,
                            color=ft.Colors.ON_SURFACE_VARIANT),
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Divider(height=1),
                order_list_view,
            ], expand=True, spacing=8),
            expand=True, padding=10, border=ft.border.all(1, ft.Colors.OUTLINE_VARIANT), border_radius=10,
        ),
        ft.Container(
            content=ft.Column([
                ft.Row([
                    output_dir_field,
                    category_field,
                    ft.OutlinedButton("选择输出目录", icon=ft.Icons.FOLDER_OPEN, height=36,
                                      style=compact_button_style, on_click=on_pick_output_directory),
                    start_generation_btn,
                ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.ExpansionTile(
                    title=ft.Text("高级选项", size=12),
                    controls=[ft.Row([output_name_column_field], spacing=10)],
                    initially_expanded=False,
                ),
            ], spacing=6),
            padding=10, border=ft.border.all(1, ft.Colors.OUTLINE_VARIANT), border_radius=10,
        ),
    ], expand=True)

    template_library_view = ft.Column([
        ft.Row([
            ft.Row([
                ft.OutlinedButton("返回订单工作台", icon=ft.Icons.ARROW_BACK, height=36,
                                  style=compact_button_style, on_click=lambda e: show_config_page()),
                ft.Text("模板库管理", size=20, weight=ft.FontWeight.BOLD),
            ], tight=True),
            ft.Row([
                ft.TextButton("切换模板库", icon=ft.Icons.FOLDER_OPEN, on_click=on_pick_template_directory),
                ft.TextButton("重新扫描", icon=ft.Icons.REFRESH, on_click=lambda e: rescan(force=True)),
                ft.TextButton("清理无效记录", icon=ft.Icons.CLEANING_SERVICES_OUTLINED,
                              tooltip="删除已不存在模板的档案和不用的模板库配置（先列清单再确认）",
                              on_click=lambda e: show_cleanup_dialog()),
            ], tight=True),
        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        ft.Text("可以选一个总文件夹，各品类模板放在下面的子文件夹里（如 铝板画、毛毯），一次全部读到；"
                "SKU 和尺寸从文件名识别。", size=12,
                color=ft.Colors.ON_SURFACE_VARIANT),
        template_library_path_status,
        ft.Container(content=sku_list_view, expand=True, padding=8,
                     border=ft.border.all(1, ft.Colors.OUTLINE_VARIANT), border_radius=10),
    ], expand=True)

    # ================= 任务处理页 =================
    task_title = ft.Text("", size=18, weight=ft.FontWeight.BOLD)
    task_subtitle = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
    progress = ft.ProgressBar(value=0, expand=True)
    progress_text = ft.Text("", size=13)
    log_list = ft.ListView(expand=True, spacing=2, padding=8, auto_scroll=False)
    back_btn = ft.OutlinedButton(
        "返回订单工作台",
        icon=ft.Icons.ARROW_BACK,
        height=36,
        style=compact_button_style,
        disabled=True,
    )
    open_out_btn = ft.OutlinedButton(
        "打开输出目录",
        icon=ft.Icons.FOLDER_OPEN,
        height=36,
        style=compact_button_style,
        on_click=lambda e: open_in_file_manager(output_dir_field.value),
    )

    task_view = ft.Column([
        ft.Row([task_title, back_btn], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        task_subtitle,
        ft.Row([ft.Text("进度", size=13), progress]),
        progress_text,
        ft.Container(
            content=log_list, expand=True,
            border=ft.border.all(1, ft.Colors.OUTLINE_VARIANT), border_radius=6,
        ),
        ft.Row([open_out_btn]),
    ], expand=True)

    def show_config_page(e=None):
        page.controls.clear()
        page.add(config_view)

    def show_template_library(e=None):
        page.controls.clear()
        page.add(template_library_view)

    def show_task_page():
        page.controls.clear()
        page.add(task_view)

    back_btn.on_click = show_config_page

    def log(message: str, error: bool = False):
        log_list.controls.insert(0, ft.Text(
            message, size=12, selectable=True,
            color=ft.Colors.ERROR if error else None,
        ))
        del log_list.controls[MAX_LOG_LINES:]
        page.update()

    def normalize_configured_selectors(config: dict) -> list[dict]:
        selectors: list[dict] = []
        for item in config.get("selectors", []):
            enabled = bool(item.get("enabled"))
            column = str(item.get("column") or "").strip()
            candidates = [
                {
                    "path": [str(p) for p in candidate.get("path") or []],
                    "value": str(candidate.get("value") or "").strip(),
                }
                for candidate in item.get("candidates", [])
                if candidate.get("path") and str(candidate.get("value") or "").strip()
            ]
            if not enabled or not column or not candidates:
                continue
            selectors.append({
                "parent": [str(p) for p in item.get("parent") or []],
                "enabled": enabled,
                "column": column,
                "candidates": candidates,
                "relative_text_aliases": dict(item.get("relative_text_aliases") or {}),
            })
        return selectors

    def configured_selectors(tpl: templates.Template) -> list[dict]:
        return normalize_configured_selectors(group_config_for(tpl))

    def configured_smart_selectors(
        tpl: templates.Template, smart_path: list[str],
    ) -> list[dict]:
        return normalize_configured_selectors(smart_config_for(tpl, smart_path))

    def selector_managed_text_paths(tpl: templates.Template) -> set[str]:
        managed_paths: set[str] = set()
        for selector in configured_selectors(tpl):
            rel_aliases = selector.get("relative_text_aliases") or {}
            if not rel_aliases:
                continue
            for candidate in selector["candidates"]:
                base_path = list(candidate["path"])
                for rel in rel_aliases:
                    rel_parts = [part for part in str(rel).split("/") if part]
                    if rel_parts:
                        managed_paths.add(path_key(base_path + rel_parts))

        return managed_paths

    def smart_selector_managed_text_paths(
        tpl: templates.Template, smart_path: list[str],
    ) -> set[str]:
        managed_paths: set[str] = set()
        for selector in configured_smart_selectors(tpl, smart_path):
            for candidate in selector["candidates"]:
                base_path = list(candidate["path"])
                for rel in selector.get("relative_text_aliases") or {}:
                    rel_parts = [part for part in str(rel).split("/") if part]
                    if rel_parts:
                        managed_paths.add(path_key(base_path + rel_parts))
        return managed_paths

    # ---------- 替换文案构造(应用字段映射) ----------
    def build_default_path_replacements(tpl: templates.Template, row, vf: str) -> list[dict]:
        aliases = aliases_for(tpl.sku)
        template_config = group_config_for(tpl)
        managed_paths = selector_managed_text_paths(tpl)
        path_replacements: list[dict] = []
        seen_paths: set[str] = set()
        for layer in tpl.all_layers:
            if layer.get("kind") != "type" or not bool(layer.get("visible", True)):
                continue
            parts = layer_path_parts(layer)
            key = path_key(parts)
            if not parts or key in managed_paths or key in seen_paths:
                continue
            seen_paths.add(key)
            layer_name = parts[-1]
            column = (
                relative_text_alias_for_path(template_config, parts)
                or aliases.get(layer_name) or layer_name
            )
            value = row.values.get(column, "")
            if value and column != vf:
                path_replacements.append({"path": parts, "value": value})
        return path_replacements

    def visible_text_layer_paths(tpl: templates.Template) -> list[list[str]]:
        paths: list[list[str]] = []
        seen: set[str] = set()
        for layer in tpl.all_layers:
            if layer.get("kind") != "type" or not bool(layer.get("visible", True)):
                continue
            parts = layer_path_parts(layer)
            key = path_key(parts)
            if parts and key not in seen:
                seen.add(key)
                paths.append(parts)
        return paths

    def layer_path_set(tpl: templates.Template) -> set[str]:
        return {path_key(layer_path_parts(layer)) for layer in tpl.all_layers}

    def layer_by_path(tpl: templates.Template, parts: list[str]) -> dict | None:
        key = path_key(parts)
        return next(
            (layer for layer in tpl.all_layers if path_key(layer_path_parts(layer)) == key),
            None,
        )

    def pixel_layer_has_bounds(layer: dict) -> bool:
        bbox = list(layer.get("bbox") or [])
        return (
            len(bbox) == 4 and
            float(bbox[2]) > float(bbox[0]) and
            float(bbox[3]) > float(bbox[1])
        )

    def text_layer_path_set(tpl: templates.Template) -> set[str]:
        return {
            path_key(layer_path_parts(layer))
            for layer in tpl.all_layers
            if layer.get("kind") == "type"
        }

    def editable_smart_object_layers(tpl: templates.Template) -> list[tuple[dict, dict]]:
        items: list[tuple[dict, dict]] = []
        for layer in tpl.all_layers:
            if layer.get("kind") != "smartobject":
                continue
            info = dict(layer.get("smart_object") or {})
            if info.get("editable"):
                items.append((layer, info))
        return items

    def visible_smart_text_layers(smart_info: dict) -> list[dict]:
        return [
            layer for layer in smart_info.get("layers") or []
            if layer.get("kind") == "type" and bool(layer.get("visible", True))
        ]

    def smart_text_column(tpl: templates.Template, smart_path: list[str],
                          inner_layer: dict) -> str:
        inner_path = layer_path_parts(inner_layer)
        return (
            relative_text_alias_for_path(
                smart_config_for(tpl, smart_path), inner_path,
            ) or
            smart_alias_for(tpl, smart_path, inner_path) or
            (inner_path[-1] if inner_path else "")
        )

    def split_custom_replacements(replacements: list[dict]) -> tuple[list[dict], dict[tuple[str, ...], list[dict]]]:
        main_replacements: list[dict] = []
        smart_replacements: dict[tuple[str, ...], list[dict]] = {}
        for replacement in replacements:
            parts = [str(part) for part in replacement.get("path") or []]
            smart_target = parse_smart_target_parts(parts)
            if smart_target is None:
                main_replacements.append(replacement)
                continue
            smart_path, text_path = smart_target
            smart_replacements.setdefault(tuple(smart_path), []).append({
                "path": text_path,
                "value": replacement.get("value", ""),
            })
        return main_replacements, smart_replacements

    def build_group_render_params(tpl: templates.Template, row) -> tuple[list[dict], list[dict]]:
        return build_selector_actions(
            configured_selectors(tpl), row.values, text_layer_path_set(tpl),
        )

    def image_config_validation_errors(
        tpl: templates.Template, config: dict, excel_columns: list[str],
    ) -> list[str]:
        messages: list[str] = []
        layer_path = [str(part) for part in config.get("layer_path") or []]
        label = display_path(layer_path)
        column = str(config.get("column") or "").strip()
        image_dir = str(config.get("image_dir") or "").strip()
        layer = layer_by_path(tpl, layer_path)
        if not column:
            messages.append(f"图片替换图层「{label}」未填写 Excel 列名")
        elif column not in excel_columns:
            messages.append(f"图片替换列「{column}」在 Excel 中不存在（图层 {label}）")
        if not os.path.isdir(image_dir):
            messages.append(f"图片替换图层「{label}」的素材目录不存在: {image_dir or '空'}")
        if layer is None:
            messages.append(f"图片替换目标图层不存在: {label}")
        elif layer.get("kind") == "pixel" and not pixel_layer_has_bounds(layer):
            messages.append(f"图片替换目标没有有效像素边界: {label}")
        elif layer.get("kind") != "pixel" and not is_raster_image_smart_object(layer):
            messages.append(f"图片替换目标不是普通像素层或 PNG/JPEG 图片智能对象: {label}")
        mapping, duplicates = image_assets.mapping_index(config)
        if duplicates:
            messages.append(
                f"图片替换图层「{label}」存在重复 Excel 值: " + "、".join(duplicates)
            )
        if not mapping:
            messages.append(f"图片替换图层「{label}」没有配置任何图片映射")
        if os.path.isdir(image_dir):
            for value, filename in mapping.items():
                try:
                    path = image_assets.resolve_image_path(image_dir, filename)
                except ValueError as exc:
                    messages.append(f"图片替换图层「{label}」值「{value}」: {exc}")
                else:
                    if not os.path.isfile(path):
                        messages.append(
                            f"图片替换图层「{label}」值「{value}」的素材不存在: {filename}"
                        )
        return messages

    def build_image_render_params(tpl: templates.Template, row) -> list[dict]:
        actions: list[dict] = []
        for config in image_configs_for(tpl, enabled_only=True):
            column = str(config.get("column") or "").strip()
            value = str(row.values.get(column, "") or "").strip()
            if not value:
                continue
            source_path = image_assets.resolve_mapped_image(config, value)
            layer_path = [str(part) for part in config.get("layer_path") or []]
            layer = layer_by_path(tpl, layer_path) or {}
            action = {
                "path": layer_path,
                "sourcePath": source_path,
                "excelValue": value,
            }
            if is_raster_image_smart_object(layer):
                action["smartObjectId"] = str(
                    (layer.get("smart_object") or {}).get("id") or "path:" + path_key(layer_path)
                )
            actions.append(action)
        return consolidate_image_replacement_actions(actions)

    def build_smart_object_render_params(
        tpl: templates.Template, row, vf: str, custom_replacements: list[dict],
    ) -> list[dict]:
        _, custom_by_smart = split_custom_replacements(custom_replacements)
        object_id_by_path: dict[tuple[str, ...], str] = {}
        for smart_layer, smart_info in editable_smart_object_layers(tpl):
            smart_path = layer_path_parts(smart_layer)
            object_id_by_path[tuple(smart_path)] = str(
                smart_info.get("id") or "path:" + path_key(smart_path)
            )
        custom_by_object_id: dict[str, list[dict]] = {}
        for smart_path, replacements in custom_by_smart.items():
            object_id = object_id_by_path.get(smart_path)
            if object_id:
                custom_by_object_id[object_id] = custom_fields.merge_path_replacements(
                    custom_by_object_id.get(object_id, []), replacements,
                )

        tasks: list[dict] = []
        for smart_layer, smart_info in editable_smart_object_layers(tpl):
            smart_path = layer_path_parts(smart_layer)
            object_id = str(smart_info.get("id") or "path:" + path_key(smart_path))
            inner_text_paths = {
                path_key(layer_path_parts(layer))
                for layer in smart_info.get("layers") or []
                if layer.get("kind") == "type"
            }
            visibility_actions, selector_replacements = build_selector_actions(
                configured_smart_selectors(tpl, smart_path), row.values, inner_text_paths,
            )
            managed_paths = smart_selector_managed_text_paths(tpl, smart_path)
            default_replacements: list[dict] = []
            for inner_layer in visible_smart_text_layers(smart_info):
                inner_path = layer_path_parts(inner_layer)
                if path_key(inner_path) in managed_paths:
                    continue
                column = smart_text_column(tpl, smart_path, inner_layer)
                value = row.values.get(column, "") if column else ""
                if value and column != vf:
                    default_replacements.append({"path": inner_path, "value": value})
            replacements = custom_fields.merge_path_replacements(
                default_replacements,
                selector_replacements,
                custom_by_object_id.get(object_id, []),
            )
            task = {
                "smartObjectId": object_id,
                "smartObjectPath": smart_path,
                "visibilityActions": visibility_actions,
                "pathReplacements": replacements,
            }
            tasks.append(task)
        return consolidate_smart_object_tasks(tasks)

    def active_custom_text_targets(
        tpl: templates.Template, row, vf: str,
    ) -> list[custom_fields.TextTarget]:
        """Collect this row's actual text targets in PSD traversal order."""
        aliases = aliases_for(tpl.sku)
        template_config = group_config_for(tpl)
        managed_paths = selector_managed_text_paths(tpl)
        keyed_paths: dict[str, tuple[list[str], str]] = {}
        smart_keyed_paths: dict[tuple[str, ...], tuple[list[str], str]] = {}

        for layer in tpl.all_layers:
            if layer.get("kind") != "type" or not bool(layer.get("visible", True)):
                continue
            parts = layer_path_parts(layer)
            key = path_key(parts)
            if not parts or key in managed_paths:
                continue
            layer_name = parts[-1]
            column = str(
                relative_text_alias_for_path(template_config, parts)
                or aliases.get(layer_name) or layer_name
            ).strip()
            if column and column != vf:
                keyed_paths[key] = (parts, column)

        seen_smart_ids: set[str] = set()
        for smart_layer, smart_info in editable_smart_object_layers(tpl):
            smart_path = layer_path_parts(smart_layer)
            object_id = str(smart_info.get("id") or "path:" + path_key(smart_path))
            if object_id in seen_smart_ids:
                continue
            seen_smart_ids.add(object_id)
            smart_managed_paths = smart_selector_managed_text_paths(tpl, smart_path)
            smart_text_paths = {
                path_key(layer_path_parts(layer))
                for layer in smart_info.get("layers") or []
                if layer.get("kind") == "type"
            }
            for inner_layer in visible_smart_text_layers(smart_info):
                inner_path = layer_path_parts(inner_layer)
                if path_key(inner_path) in smart_managed_paths:
                    continue
                column = smart_text_column(tpl, smart_path, inner_layer)
                target_path = smart_target_parts(smart_path, inner_path)
                if column and column != vf:
                    smart_keyed_paths[tuple(target_path)] = (target_path, column)
            for selector in configured_smart_selectors(tpl, smart_path):
                excel_value = row.values.get(selector["column"], "")
                matched = [
                    candidate for candidate in selector["candidates"]
                    if candidate["value"] == excel_value
                ]
                for candidate in matched:
                    for rel, alias_column in selector["relative_text_aliases"].items():
                        alias_column = str(alias_column or "").strip()
                        rel_parts = [part for part in str(rel).split("/") if part]
                        inner_path = list(candidate["path"]) + rel_parts
                        if (not alias_column or not rel_parts or
                                path_key(inner_path) not in smart_text_paths):
                            continue
                        target_path = smart_target_parts(smart_path, inner_path)
                        smart_keyed_paths[tuple(target_path)] = (target_path, alias_column)

        text_paths = text_layer_path_set(tpl)
        for selector in configured_selectors(tpl):
            excel_value = row.values.get(selector["column"], "")
            if not excel_value:
                continue
            matched = [
                candidate for candidate in selector["candidates"]
                if candidate["value"] == excel_value
            ]
            for candidate in matched:
                for rel, alias_column in selector["relative_text_aliases"].items():
                    alias_column = str(alias_column or "").strip()
                    rel_parts = [part for part in str(rel).split("/") if part]
                    path = list(candidate["path"]) + rel_parts
                    key = path_key(path)
                    if alias_column and rel_parts and key in text_paths:
                        keyed_paths[key] = (path, alias_column)

        targets: list[custom_fields.TextTarget] = []
        for layer in tpl.all_layers:
            key = path_key(layer_path_parts(layer))
            target = keyed_paths.get(key)
            if target:
                targets.append(custom_fields.TextTarget.from_parts(*target))
            if layer.get("kind") == "smartobject":
                smart_path = layer_path_parts(layer)
                smart_info = dict(layer.get("smart_object") or {})
                if not smart_info.get("editable"):
                    continue
                for inner_layer in smart_info.get("layers") or []:
                    inner_path = layer_path_parts(inner_layer)
                    smart_target = smart_target_parts(smart_path, inner_path)
                    target = smart_keyed_paths.get(tuple(smart_target))
                    if target:
                        targets.append(custom_fields.TextTarget.from_parts(*target))
        return targets

    def resolve_row_custom_info(tpl: templates.Template, row, vf: str) -> custom_fields.Resolution:
        if not row.custom_info:
            return custom_fields.Resolution()
        return custom_fields.resolve_custom_info(
            row.custom_info,
            active_custom_text_targets(tpl, row, vf),
        )

    def custom_resolution_errors(resolution: custom_fields.Resolution) -> list[str]:
        messages = [
            f"字段「{match.target_key}」拆分得到 {match.actual_count} 个值, "
            f"但对应 {match.expected_count} 个文字图层"
            for match in resolution.count_mismatches
        ]
        messages.extend(
            f"key「{match.source_key}」以 {match.similarity:.0%} 的相似度同时匹配字段「" +
            "、".join(match.candidates) + "」"
            for match in resolution.ambiguous_matches
        )
        return messages

    def custom_resolution_notes(resolution: custom_fields.Resolution) -> list[str]:
        messages: list[str] = []
        for match in resolution.matches:
            for source_key, similarity in match.source_matches:
                if similarity < custom_fields.DEFAULT_SIMILARITY_THRESHOLD:
                    messages.append(
                        f"定制信息 key「{source_key}」通过同组相似 key 匹配字段"
                        f"「{match.target_key}」"
                    )
                elif similarity < 1.0:
                    messages.append(
                        f"定制信息 key「{source_key}」按 {similarity:.0%} 相似度匹配字段"
                        f"「{match.target_key}」"
                    )
        if resolution.unmatched_keys:
            messages.append(
                "定制信息 key「" + "、".join(resolution.unmatched_keys) +
                "」未匹配 PSD 文字字段, 已忽略"
            )
        if resolution.ignored_lines:
            messages.append(
                "定制信息内容「" + "、".join(resolution.ignored_lines) +
                "」缺少 key, 已忽略"
            )
        return list(dict.fromkeys(messages))

    # ---------- 预检 ----------
    def selector_contexts(tpl: templates.Template) -> list[tuple[str, list[str] | None, dict]]:
        contexts = [("主文档", None, selector) for selector in configured_selectors(tpl)]
        for smart_layer, _ in editable_smart_object_layers(tpl):
            smart_path = layer_path_parts(smart_layer)
            contexts.extend([
                (f"智能对象 {path_key(smart_path)}", smart_path, selector)
                for selector in configured_smart_selectors(tpl, smart_path)
            ])
        return contexts

    def preflight_items() -> list[dict[str, str]]:
        """缺模版 SKU + Excel 定制列/模版字段(含映射)匹配情况。"""
        excel = state["excel"]
        items: list[dict[str, str]] = []
        # Aluminum orders are checked by compile_plan (below); the original
        # column/alias/split checks only produce false alarms for them.
        aluminum = is_aluminum_mode()

        def warning(text: str) -> None:
            items.append({"level": "warning", "text": text})

        def error(text: str) -> None:
            items.append({"level": "error", "text": text})

        missing: dict[str, int] = {}
        involved: dict[str, templates.Template] = {}
        used_variant_fields: set[str] = set()
        used_visibility_columns: set[str] = set()
        used_image_columns: set[str] = set()
        missing_visibility_columns: set[str] = set()
        unmatched_visibility_values: dict[tuple[str, str, str, str], set[str]] = {}
        custom_matched_paths: dict[str, set[tuple[str, ...]]] = {}
        checked_image_templates: dict[str, bool] = {}
        scope_rows = render_rows()
        in_scope = {row.row_no for row in scope_rows}
        name_column = str(output_name_column_field.value or "").strip()
        seen_names: dict[str, int] = {}
        for row in scope_rows:
            vf = variant_field_for(row.sku)
            if vf:
                used_variant_fields.add(vf)
            tpls = templates_for_row(row, row.values.get(vf, "") if vf else "")
            if not tpls:
                missing[row.sku] = missing.get(row.sku, 0) + 1
            for seq in range(1, len(tpls) + 1) if will_render(row) else ():
                try:
                    name = excel_data.output_basename(row, excel.hash_mode, vf, seq,
                                                      name_column=name_column, template_count=len(tpls))
                except ValueError:
                    continue  # 命名列为空等, 生成时按原规则报错
                first_row = seen_names.setdefault(excel_data.output_name_key(name), row.row_no)
                if first_row != row.row_no:
                    warning(f"Excel 第 {row.row_no} 行的输出文件名 {name} 与第 {first_row} 行重复"
                            "（文件名不区分大小写），生成时该行会判为失败，避免覆盖")
            for t in tpls:
                involved[t.path] = t
                if aluminum:
                    continue
                image_configs = image_configs_for(t, enabled_only=True)
                for image_config in image_configs:
                    column = str(image_config.get("column") or "").strip()
                    if column:
                        used_image_columns.add(column)
                image_configs_valid = checked_image_templates.get(t.path)
                if image_configs_valid is None:
                    image_errors = [
                        message
                        for image_config in image_configs
                        for message in image_config_validation_errors(
                            t, image_config, excel.custom_columns,
                        )
                    ]
                    configured_paths = [
                        path_key([str(part) for part in config.get("layer_path") or []])
                        for config in image_configs
                    ]
                    duplicate_paths = sorted({
                        path for path in configured_paths
                        if path and configured_paths.count(path) > 1
                    })
                    if duplicate_paths:
                        image_errors.append(
                            "同一图片替换目标被重复配置: " + "、".join(duplicate_paths)
                        )
                    for message in image_errors:
                        error(f"模版 {t.sku}/{t.filename}: {message}")
                    image_configs_valid = not image_errors
                    checked_image_templates[t.path] = image_configs_valid
                if image_configs_valid:
                    try:
                        build_image_render_params(t, row)
                    except ValueError as exc:
                        error(
                            f"Excel 第 {row.row_no} 行 · SKU {row.sku} · "
                            f"模版 {t.filename}: {exc}"
                        )
                custom_resolution = resolve_row_custom_info(t, row, vf)
                if row.custom_info:
                    matched_paths = custom_matched_paths.setdefault(t.path, set())
                    for match in custom_resolution.matches:
                        matched_paths.update(tuple(target.path) for target in match.targets)
                    for message in custom_resolution_errors(custom_resolution):
                        error(
                            f"Excel 第 {row.row_no} 行 · SKU {row.sku} · "
                            f"模版 {t.filename}: {message}"
                        )
                    for message in custom_resolution_notes(custom_resolution):
                        warning(
                            f"Excel 第 {row.row_no} 行 · SKU {row.sku} · "
                            f"模版 {t.filename}: {message}"
                        )
                try:
                    build_smart_object_render_params(
                        t, row, vf, custom_resolution.replacements,
                    )
                except ValueError as exc:
                    error(
                        f"Excel 第 {row.row_no} 行 · SKU {row.sku} · "
                        f"模版 {t.filename}: {exc}"
                    )
                for context, _, selector in selector_contexts(t):
                    used_visibility_columns.add(selector["column"])
                    if selector["column"] not in excel.custom_columns:
                        missing_visibility_columns.add(selector["column"])
                    for rel, column in selector["relative_text_aliases"].items():
                        column = str(column or "").strip()
                        if column:
                            used_visibility_columns.add(column)
                            if column not in excel.custom_columns and not excel.custom_info_column:
                                missing_visibility_columns.add(column)
                    value = row.values.get(selector["column"], "")
                    if value and not any(c["value"] == value for c in selector["candidates"]):
                        unmatched_visibility_values.setdefault(
                            (t.sku, t.filename, context, selector["column"]), set()
                        ).add(value)
        if missing:
            lines = "".join(f"\n  · {sku} ({n} 行)" for sku, n in sorted(missing.items()))
            warning(f"以下 {len(missing)} 个 SKU 缺少模版, 对应行将跳过:{lines}")

        if is_aluminum_mode():
            # Only ready orders render; the others are skipped and reported
            # instead of blocking the whole batch.
            skipped = [
                f"第 {order.row_no} 行 {order.sku} / {order.size_key or '尺寸未知'}：{order.status_reason}"
                for order in state.get("orders") or []
                if order.status in {"invalid", "needs_review"} and order.row_no in in_scope
            ]
            if skipped:
                warning(f"以下 {len(skipped)} 行模板未审核或订单异常，本次跳过：\n  · " + "\n  · ".join(skipped))
            failing: list[str] = []
            for order in state.get("orders") or []:
                if order.status != "ready" or order.row_no not in in_scope:
                    continue
                for tpl in templates_for_row(order.row):
                    plan = order_pipeline.compile_plan(
                        tpl, copy.deepcopy(order.row), aluminum_plan_cfg(tpl), template_dir_field.value, strict=False,
                    )
                    if not plan.get("ok"):
                        failing.append(f"第 {order.row_no} 行 {tpl.filename}：" + "；".join(plan.get("errors") or []))
            if failing:
                warning(f"以下 {len(failing)} 行生成计划未通过检查，将记为失败（其他行照常生成）：\n  · " + "\n  · ".join(failing))

        used_columns: set[str] = set()  # also read by the unused-column check below
        if not aluminum:
            # 配置了切换模版字段但 Excel 没这一列
            bad_vf = {vf for vf in used_variant_fields if vf not in excel.custom_columns}
            if bad_vf:
                warning("切换模版字段「" + "、".join(sorted(bad_vf)) +
                        "」在 Excel 中无同名列, 相关 SKU 将使用默认模版")

            if missing_visibility_columns:
                warning("图层切换列「" + "、".join(sorted(missing_visibility_columns)) +
                        "」在 Excel 中无同名列, 相关图层切换/组内文字配置将不生效")

            for (sku, filename, context, column), values in sorted(
                unmatched_visibility_values.items()
            ):
                warning(
                    f"模版 {sku}/{filename} · {context} 的图层切换列「{column}」存在未配置值「" +
                    "、".join(sorted(values)) + "」, 这些行将保留 PSD 默认显隐"
                )

            for t in involved.values():
                paths = layer_path_set(t)
                missing_paths = [
                    path_key(path)
                    for selector in configured_selectors(t)
                    for path in [selector["parent"], *[c["path"] for c in selector["candidates"]]]
                    if path and path_key(path) not in paths
                ]
                if missing_paths:
                    warning(
                        f"模版 {t.sku}/{t.filename} 的显隐目标「" +
                        "、".join(missing_paths) + "」不存在"
                    )
                for smart_layer, smart_info in editable_smart_object_layers(t):
                    smart_path = layer_path_parts(smart_layer)
                    inner_paths = {
                        path_key(layer_path_parts(layer))
                        for layer in smart_info.get("layers") or []
                    }
                    missing_inner_paths = [
                        path_key(path)
                        for selector in configured_smart_selectors(t, smart_path)
                        for path in [
                            selector["parent"],
                            *[candidate["path"] for candidate in selector["candidates"]],
                        ]
                        if path and path_key(path) not in inner_paths
                    ]
                    if missing_inner_paths:
                        warning(
                            f"模版 {t.sku}/{t.filename} 的智能对象「{path_key(smart_path)}」"
                            "内部显隐目标「" + "、".join(missing_inner_paths) + "」不存在"
                        )

            # 字段(图层, 经映射后)与 Excel 列的匹配情况
            for t in involved.values():
                aliases = aliases_for(t.sku)
                template_config = group_config_for(t)
                managed_paths = selector_managed_text_paths(t)
                unmatched = []
                duplicate_names: dict[str, list[str]] = {}
                for parts in visible_text_layer_paths(t):
                    layer = parts[-1]
                    duplicate_names.setdefault(layer, []).append(path_key(parts))
                    if path_key(parts) in managed_paths:
                        continue
                    column = (
                        relative_text_alias_for_path(template_config, parts)
                        or aliases.get(layer) or layer
                    )
                    if column in excel.custom_columns:
                        used_columns.add(column)
                    elif tuple(parts) not in custom_matched_paths.get(t.path, set()):
                        label = path_key(parts)
                        unmatched.append(label if column == layer else f"{label}(←{column})")
                seen_smart_ids: set[str] = set()
                for smart_layer, smart_info in editable_smart_object_layers(t):
                    smart_path = layer_path_parts(smart_layer)
                    object_id = str(smart_info.get("id") or "path:" + path_key(smart_path))
                    if object_id in seen_smart_ids:
                        continue
                    seen_smart_ids.add(object_id)
                    smart_managed_paths = smart_selector_managed_text_paths(t, smart_path)
                    for inner_layer in visible_smart_text_layers(smart_info):
                        inner_path = layer_path_parts(inner_layer)
                        if path_key(inner_path) in smart_managed_paths:
                            continue
                        layer = inner_path[-1] if inner_path else ""
                        label = f"{path_key(smart_path)} › {path_key(inner_path)}"
                        duplicate_names.setdefault(layer, []).append(label)
                        column = smart_text_column(t, smart_path, inner_layer)
                        target = tuple(smart_target_parts(smart_path, inner_path))
                        if column in excel.custom_columns:
                            used_columns.add(column)
                        elif target not in custom_matched_paths.get(t.path, set()):
                            unmatched.append(
                                label if column == layer else f"{label}(←{column})"
                            )
                if unmatched:
                    warning(
                        f"模版 {t.sku}/{t.filename} 的字段「" + "、".join(unmatched) +
                        "」无对应 Excel 列, 将保留默认文案")
                repeated = [
                    f"{name}: " + "、".join(paths)
                    for name, paths in duplicate_names.items()
                    if len(paths) > 1
                ]
                if repeated:
                    warning(
                        f"模版 {t.sku}/{t.filename} 存在同名文字图层, 将按完整路径分别处理: " +
                        "；".join(repeated)
                    )

        name_column = str(output_name_column_field.value or "").strip()
        name_column_exists = not name_column or any(
            header == name_column
            or (
                name_column.casefold() in {"sku", "hash"}
                and header.casefold() == name_column.casefold()
            )
            for header in excel.headers
        )
        if name_column and not name_column_exists:
            error(f"图片命名列「{name_column}」在 Excel 中不存在")
        elif name_column:
            missing_name_rows: list[int] = []
            generated_names: dict[str, list[int]] = {}
            for row in scope_rows:
                # 只查本次会生成的订单: 待审核/缺模板/异常订单不出图,
                # 它们的命名列为空不该挡住整批（2026-10-14 用户确认）
                if not will_render(row):
                    continue
                vf = variant_field_for(row.sku)
                field_value = row.values.get(vf, "") if vf else ""
                row_templates = templates_for_row(row, field_value)
                if not row_templates:
                    continue
                try:
                    names = [
                        excel_data.output_basename(
                            row, excel.hash_mode, vf, seq,
                            name_column=name_column,
                            template_count=len(row_templates),
                        )
                        for seq in range(1, len(row_templates) + 1)
                    ]
                except ValueError:
                    missing_name_rows.append(row.row_no)
                    continue
                for name in names:
                    generated_names.setdefault(name.casefold(), []).append(row.row_no)
            if missing_name_rows:
                error(
                    f"图片命名列「{name_column}」在 Excel 行「" +
                    "、".join(str(row_no) for row_no in missing_name_rows) +
                    "」为空或无法生成有效文件名"
                )
            duplicate_names = {
                name: row_numbers
                for name, row_numbers in generated_names.items()
                if len(row_numbers) > 1
            }
            if duplicate_names:
                details = "；".join(
                    f"{name}（Excel 行 " +
                    "、".join(str(row_no) for row_no in row_numbers) + "）"
                    for name, row_numbers in sorted(duplicate_names.items())
                )
                error(f"图片命名列「{name_column}」产生重复输出文件名: {details}")

        unused_cols = [c for c in excel.custom_columns
                       if c not in used_columns
                       and c not in used_variant_fields
                       and c not in used_visibility_columns
                       and c not in used_image_columns
                       and c != name_column]
        if unused_cols and involved and not aluminum:
            warning("列「" + "、".join(unused_cols) + "」未被任何涉及模版的字段使用")

        # 交易编号命名模式: 同一交易编号的多行或多模版会生成同名文件并互相覆盖。
        if excel.order_mode and not name_column:
            order_counts: dict[str, int] = {}
            for row in scope_rows:
                order_counts[row.order_no] = order_counts.get(row.order_no, 0) + 1
            dups = {order: count for order, count in order_counts.items() if count > 1}
            if dups:
                lines = "".join(f"\n  · {order} ({count} 行)"
                                for order, count in sorted(dups.items()))
                warning(f"以下交易编号有多行, 输出文件名相同; 生成时只出第一行, 后面的行判为失败（不会覆盖）:{lines}")
        return items

    def check_fonts_then_start():
        """开跑前查缺字体（2026-10-14 用户需求）：先装模板库里的字体，再看本次要用的
        模板还缺哪些字体；缺了就弹窗列出，由用户选择关闭去补字体或继续生成。

        Photoshop 已经开着时直接问它能用哪些字体（最准）；没开或没响应时扫描本机
        字体文件夹（不为查字体去启动 Photoshop）。查询放后台线程，界面显示“正在检查字体”。
        """
        involved: dict[str, templates.Template] = {}
        orders: dict[str, int] = {}
        for row in render_rows():
            if not will_render(row):
                continue
            vf = variant_field_for(row.sku)
            for t in templates_for_row(row, row.values.get(vf, "") if vf else ""):
                involved.setdefault(t.path, t)
                orders[t.path] = orders.get(t.path, 0) + 1
        checking = ft.AlertDialog(
            modal=True,
            title=ft.Text("正在检查字体…"),
            content=ft.Row([ft.ProgressRing(width=20, height=20, stroke_width=2),
                            ft.Text("安装模板库字体，并核对本次模板用到的字体", size=13)], spacing=12),
        )
        page.open(checking)
        page.update()

        def work():
            missing: list[dict] = []
            source = ""
            try:
                report = font_installer.install_library_fonts(template_dir_field.value)
            except Exception as exc:  # noqa: BLE001
                report = {"installed": [], "failed": [("字体检查", str(exc))]}
            state["pending_font_report"] = report
            try:
                ps_fonts = renderer.photoshop_fonts()
                if ps_fonts is not None:
                    # 刚装进字体目录的库内字体，开着的 Photoshop 要重启才认；
                    # 开跑时程序会自动重启它，这里算作已有。
                    failed = {label for label, _ in report.get("failed") or []}
                    library = set()
                    for path in font_installer.find_library_fonts(template_dir_field.value):
                        if os.path.basename(path) not in failed:
                            library |= font_installer.font_names(path)
                    installed = {name.casefold() for name in ps_fonts} | library
                    source = "按 Photoshop 当前能用的字体核对"
                else:
                    installed = None
                    source = "Photoshop 没有打开，按本机字体文件夹核对"
                missing = font_installer.missing_template_fonts(involved.values(), installed=installed)
            except Exception as exc:  # noqa: BLE001 - 查不了字体不阻止生成
                state["pending_font_report"].setdefault("failed", []).append(("缺字体检查", str(exc)))
            close_dialog(checking)
            if not missing:
                start_task()
                return
            show_missing_fonts(missing, orders, source)

        threading.Thread(target=work, daemon=True).start()

    def show_missing_fonts(missing: list[dict], orders: dict[str, int], source: str) -> None:
        fonts = sorted({font for item in missing for font in item["missing"]})
        lines: list[ft.Control] = [ft.Text(
            f"本次要生成的订单里，有 {len(missing)} 个模板用到的字体本机没有：{'、'.join(fonts)}。"
            f"（{source}）\n缺字体时 Photoshop 会自动换成别的字体出图，不会报错，图上的字体会不对。",
            size=13,
        )]
        for item in missing:
            detail = "\n".join(
                f"    缺 {font}（图层：{'、'.join(dict.fromkeys(layers))}）"
                for font, layers in sorted(item["missing"].items())
            )
            lines.append(ft.Container(
                content=ft.Text(f"{item['template']}（{orders.get(item['path'], 0)} 单）\n{detail}",
                                size=12, selectable=True),
                bgcolor=ft.Colors.TERTIARY_CONTAINER, border_radius=4,
                padding=ft.padding.symmetric(horizontal=10, vertical=8),
            ))
        lines.append(ft.Text(
            "补字体：把字体文件（.otf/.ttf）放进模板库文件夹，再点“预检并生成”会自动安装；"
            "Adobe Fonts 的字体在 Creative Cloud 里激活。自己手动装过字体的，要重启 Photoshop 才生效。",
            size=12, color=ft.Colors.ON_SURFACE_VARIANT,
        ))
        dlg = ft.AlertDialog(
            title=ft.Text("缺少字体"),
            content=ft.Container(content=ft.ListView(controls=lines, spacing=10), width=680, height=360),
        )

        def on_continue(e):
            close_dialog(dlg)
            state["missing_font_notes"] = [
                f"{item['template']} 缺字体 {'、'.join(sorted(item['missing']))}" for item in missing
            ]
            start_task()

        dlg.actions = [
            ft.TextButton("关闭（去补充字体）", height=36, style=text_button_style,
                          on_click=lambda e: close_dialog(dlg)),
            ft.FilledButton("继续生成", icon=ft.Icons.PLAY_ARROW, height=36,
                            style=compact_button_style, on_click=on_continue),
        ]
        page.open(dlg)
        page.update()

    def confirm_and_start():
        items = preflight_items()
        if not items:
            check_fonts_then_start()
            return
        errors = [item for item in items if item["level"] == "error"]
        warnings = [item for item in items if item["level"] == "warning"]
        blocking = bool(errors)

        def build_preflight_section(
            title: str,
            section_items: list[dict[str, str]],
            color: str,
            bgcolor: str,
            helper: str,
        ) -> ft.Control:
            controls: list[ft.Control] = [
                ft.Text(
                    f"{title}（{len(section_items)}）",
                    size=13,
                    weight=ft.FontWeight.BOLD,
                    color=color,
                ),
                ft.Text(helper, size=12, color=ft.Colors.ON_SURFACE_VARIANT),
            ]
            controls.extend([
                ft.Container(
                    content=ft.Text(item["text"], size=13, selectable=True),
                    bgcolor=bgcolor,
                    border_radius=4,
                    padding=ft.padding.symmetric(horizontal=10, vertical=8),
                )
                for item in section_items
            ])
            return ft.Column(controls, spacing=6)

        section_controls: list[ft.Control] = []
        if errors:
            section_controls.append(build_preflight_section(
                "错误",
                errors,
                ft.Colors.ERROR,
                ft.Colors.ERROR_CONTAINER,
                "以下问题会阻止生成, 请修复后重新选择 Excel。",
            ))
        if warnings:
            section_controls.append(build_preflight_section(
                "警告",
                warnings,
                ft.Colors.TERTIARY,
                ft.Colors.TERTIARY_CONTAINER,
                "以下问题不会阻止生成, 但可能导致部分行跳过或输出与预期不一致。",
            ))

        dlg = ft.AlertDialog(
            title=ft.Text("预检未通过" if blocking else "开始前请确认"),
            content=ft.Container(
                content=ft.ListView(
                    controls=section_controls,
                    spacing=14,
                ),
                width=680, height=360,
            ),
        )

        def on_continue(e):
            close_dialog(dlg)
            check_fonts_then_start()

        dlg.actions = [
            ft.TextButton(
                "关闭" if blocking else "取消",
                on_click=lambda e: close_dialog(dlg),
                height=36,
                style=text_button_style,
            ),
        ]
        if not blocking:
            dlg.actions.append(ft.FilledButton(
                "继续生成",
                icon=ft.Icons.PLAY_ARROW,
                height=36,
                style=compact_button_style,
                on_click=on_continue,
            ))
        page.open(dlg)

    # ---------- 渲染循环(后台线程) ----------
    def show_summary(success_images: int, failed_rows: int,
                     report_path: str | None, log_path: str | None, skipped_rows: int = 0):
        body = [ft.Text(f"成功: {success_images} 张"), ft.Text(f"失败: {failed_rows} 行")]
        if skipped_rows:
            body.append(ft.Text(f"未生成（缺模板/待审核等）: {skipped_rows} 行"))
        folder = os.path.dirname(log_path or report_path or "") or output_dir_field.value
        body.append(ft.Text(f"图片在 {folder} 下，按 店铺名/品类 分文件夹"))
        if report_path:
            body.append(ft.Text(f"{os.path.basename(report_path)} 已写入该文件夹"))
        if log_path:
            body.append(ft.Text(f"{os.path.basename(log_path)} 已写入该文件夹"))
        dlg = ft.AlertDialog(title=ft.Text("生成完成"), content=ft.Column(body, tight=True))
        dlg.actions = [
            ft.TextButton(
                "打开输出目录",
                icon=ft.Icons.FOLDER_OPEN,
                height=36,
                style=text_button_style,
                on_click=lambda e: open_in_file_manager(folder),
            ),
            ft.TextButton(
                "关闭",
                height=36,
                style=text_button_style,
                on_click=lambda e: close_dialog(dlg),
            ),
        ]
        page.open(dlg)

    def pick_templates_with_notes(row, vf: str, field_value: str) -> tuple[list[templates.Template], list[str]]:
        if is_aluminum_mode():
            normalized = normalized_order_for(row)
            if not normalized or not normalized.size_key:
                return [], [f"Excel 第 {row.row_no} 行尺寸无法解析，不能选择模板"]
            candidates = templates.pick_by_size(
                state["index"], row.sku, normalized.size_keys, field_value,
            )
            if not candidates:
                return [], [
                    f"SKU {row.sku} 没有严格匹配尺寸 {normalized.size_key} 的模板"
                ]
            if len(candidates) > 1:
                return [], [
                    f"SKU {row.sku} / {normalized.size_key} 命中多个模板，需要人工确认："
                    + template_places(candidates)
                ]
            return candidates, []
        candidates = state["index"].get(row.sku) or []
        if field_value and candidates:
            exact = [t for t in candidates if t.field_value == field_value]
            if not exact:
                defaults = [t for t in candidates if t.field_value == ""]
                if defaults:
                    return defaults, [
                        f"切换模版字段「{vf}」值「{field_value}」未匹配变体模版, 已回退默认模版"
                    ]
                return [], [
                    f"切换模版字段「{vf}」值「{field_value}」未匹配变体模版, 且没有默认模版"
                ]
        return templates.pick(state["index"], row.sku, field_value), []

    def aluminum_plan_cfg(tpl: templates.Template) -> dict:
        """Settings plus the approved template-library profile (text mappings)."""
        profile = template_profiles.get_profile(
            state.get("profile_store") or {}, tpl.sku, tpl.size_key, tpl.filename, tpl.path,
        )
        if profile and profile.get("status") == "approved":
            return {**cfg, "profile": profile}
        return cfg

    def ai_text_replacements_for(tpl: templates.Template, row) -> list[dict]:
        """Use AI only as an optional field-to-text-path alias layer.

        The actual render pipeline remains the original deterministic pipeline;
        these replacements simply fill the mapping that the user approved.
        """
        if not is_aluminum_mode():
            return []
        profile = template_profiles.get_profile(
            state.get("profile_store") or {}, tpl.sku, tpl.size_key, tpl.filename, tpl.path,
        ) or {}
        normalized = normalized_order_for(row)
        fields: dict[str, object] = {}
        if normalized:
            fields.update(normalized.custom_fields)
        for key, value in row.values.items():
            fields.setdefault(key, [value])
        replacements: list[dict] = []
        for mapping in profile.get("text_mappings") or []:
            source_key = str(mapping.get("source_key") or "")
            values = fields.get(source_key, [])
            value = values[0] if isinstance(values, list) and values else values
            if not str(value or "").strip():
                continue
            if mapping.get("smart_object_path"):
                # Smart-object text paths are already handled by the original
                # smart-object pipeline; keep this path out of the main list.
                continue
            replacements.append({
                "path": [str(part) for part in mapping.get("target_path") or []],
                "value": str(value),
            })
        return replacements

    def group_default_notes(tpl: templates.Template, row) -> list[str]:
        notes: list[str] = []
        for context, _, selector in selector_contexts(tpl):
            column = selector["column"]
            excel_value = row.values.get(column, "")
            if not excel_value:
                continue
            if not any(candidate["value"] == excel_value for candidate in selector["candidates"]):
                notes.append(
                    f"图层切换列「{column}」值「{excel_value}」未匹配候选, "
                    f"{context} / {display_path(selector['parent'])} 保留 PSD 默认显隐"
                )
        return notes

    def write_render_log(out_dir: str, records: list[dict], category: str = "") -> str:
        log_path = os.path.join(out_dir, f"生成日志-{category}.csv" if category else "生成日志.csv")
        with open(log_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["行号", "SKU", "模版", "输出文件", "状态", "记录"],
            )
            writer.writeheader()
            writer.writerows(records)
        return log_path

    def add_render_record(records: list[dict], row_no, sku: str, template_name: str,
                          output_name: str, status: str, messages: list[str] | str) -> None:
        if isinstance(messages, list):
            message = "；".join([m for m in messages if m])
        else:
            message = messages
        records.append({
            "行号": row_no,
            "SKU": sku,
            "模版": template_name,
            "输出文件": output_name,
            "状态": status,
            "记录": message,
        })

    def prepare_fonts_before_render() -> None:
        """Install fonts added to the library since the scan; restart Photoshop if needed."""
        # check_fonts_then_start already installed them right before the start.
        report = state.pop("pending_font_report", None)
        if report is None:
            try:
                report = font_installer.install_library_fonts(template_dir_field.value)
            except Exception as exc:  # noqa: BLE001
                log(f"⚠ 字体检查失败: {exc}", error=True)
                report = {"installed": [], "failed": []}
        for note in state.pop("missing_font_notes", None) or []:
            log(f"⚠ 用户选择缺字体继续生成：{note}（Photoshop 会换成其他字体）", error=True)
        for name in report.get("installed") or []:
            log(f"已安装模板库字体: {name}")
            state["fonts_need_ps_restart"] = True
        for name, reason in report.get("failed") or []:
            log(f"⚠ 字体未能安装: {name}（{reason}）", error=True)
        if not state.get("fonts_need_ps_restart"):
            return
        if renderer.photoshop_running():
            log("检测到新安装的字体，正在退出 Photoshop（如有未保存的文档，请在 Photoshop 中处理）…")
            if not renderer.quit_photoshop():
                log("⚠ Photoshop 未退出，新字体可能不生效；请手动重启 Photoshop 后重新生成", error=True)
                return
            log("Photoshop 已退出，生成时会自动重新打开")
        state["fonts_need_ps_restart"] = False

    def mark_generated_rows(row_numbers: set[int]) -> None:
        """Fill the rows that were fully generated yellow in the order workbook (backup first)."""
        path = state.get("excel_path") or ""
        if not row_numbers or not path:
            return
        lock = excel_marker.lock_file(path)
        try:
            result = excel_marker.mark_rows(path, row_numbers)
        except Exception as exc:  # noqa: BLE001 - marking must never lose the images already made
            log(f"⚠ 订单表标黄失败（{exc}）：如果表格正在 WPS/Excel 中打开，请关闭后重新生成；"
                f"已生成的行号：{'、'.join(map(str, sorted(row_numbers)))}", error=True)
            return
        log(f"订单表已把生成成功的 {len(result['marked'])} 行标黄（原表备份：{result['backup']}）")
        if lock:
            log("⚠ 订单表正在 WPS/Excel 中打开：请关闭后重新打开查看标记，不要在之前打开的窗口里保存，否则会覆盖标记",
                error=True)

    def run_render():
        excel = state["excel"]
        root_dir = output_dir_field.value
        run_day = date.today()
        out_dir = output_layout.day_dir(root_dir, run_day)
        os.makedirs(out_dir, exist_ok=True)
        category = output_layout.folder_name(category_field.value, "") or \
            output_layout.category_from_excel(state.get("excel_path") or "")
        generated_rows: set[int] = set()
        prepare_fonts_before_render()
        name_column = str(output_name_column_field.value or "").strip()
        scope = render_rows()
        partial = len(scope) != len(excel.rows)
        # Rows Excel parsing skipped belong to a full run only, not to "生成选中".
        parse_skipped = [] if partial else list(excel.skipped)
        failures: list[tuple] = [(row_no, "", reason) for row_no, reason in parse_skipped]
        render_records: list[dict] = []
        for row_no, reason in parse_skipped:
            add_render_record(render_records, row_no, "", "", "", "跳过", reason)
        if partial:
            log(f"只生成勾选的 {len(scope)} 条订单")
        success_images = 0
        failed_rows = len(parse_skipped)
        # Aluminum mode renders only the orders that are ready; the others
        # (missing template, waiting for review, ...) were already listed by the
        # pre-check and go straight to the failure report, without a log line each.
        rows = list(scope)
        if is_aluminum_mode():
            not_ready = [(row, normalized_order_for(row)) for row in rows if not will_render(row)]
            skipped_ids = {id(row) for row, _ in not_ready}
            rows = [row for row in rows if id(row) not in skipped_ids]
            reasons: dict[str, int] = {}
            for row, order in not_ready:
                reason = f"未生成：{(order.status_reason or order.status) if order else '订单无法解析'}"
                failures.append((row.row_no, row.sku, reason))
                add_render_record(render_records, row.row_no, row.sku, "", "", "跳过", reason)
                label = _order_status_label(order.status) if order else "异常"
                reasons[label] = reasons.get(label, 0) + 1
            if not_ready:
                detail = "、".join(f"{label} {count} 条" for label, count in reasons.items())
                log(f"本次只生成 {len(rows)} 条可生成订单；跳过 {len(not_ready)} 条（{detail}），明细见 失败报告-{category}.csv")
        skipped_rows = len(scope) - len(rows)
        total = len(rows)
        log_path = None
        used_names: dict[str, int] = {}  # 输出文件名(不区分大小写) -> 先占用的 Excel 行号
        try:
            for i, row in enumerate(rows, start=1):
                vf = variant_field_for(row.sku)
                field_value = row.values.get(vf, "") if vf else ""
                tpls, row_notes = pick_templates_with_notes(row, vf, field_value)
                for note in row_notes:
                    log(f"[{i}] ⚠ SKU {row.sku}: {note}", error=True)
                if not tpls:
                    reason = "；".join(row_notes) if row_notes else "缺少模版"
                    failures.append((row.row_no, row.sku, reason))
                    failed_rows += 1
                    add_render_record(render_records, row.row_no, row.sku, "", "", "失败", reason)
                    log(f"[{i}] ✗ SKU {row.sku} {reason}, 已跳过", error=True)
                else:
                    row_failed = False
                    for seq, tpl in enumerate(tpls, start=1):
                        try:
                            name = excel_data.output_basename(
                                row, excel.hash_mode, vf, seq,
                                name_column=name_column,
                                template_count=len(tpls),
                            )
                        except ValueError as exc:
                            name, reason = "", str(exc)
                        else:
                            first_row = used_names.setdefault(excel_data.output_name_key(name), row.row_no)
                            reason = "" if first_row == row.row_no else (
                                f"输出文件名 {name} 与第 {first_row} 行重复（文件名不区分大小写），为避免覆盖未生成")
                        if reason:
                            row_failed = True
                            failures.append((row.row_no, row.sku, reason))
                            add_render_record(
                                render_records, row.row_no, row.sku,
                                tpl.filename, name, "失败", reason,
                            )
                            log(f"[{i}] ✗ SKU {row.sku} · {reason}", error=True)
                            continue
                        aluminum = is_aluminum_mode()
                        custom_resolution = None if aluminum else resolve_row_custom_info(tpl, row, vf)
                        resolution_errors = [] if aluminum else custom_resolution_errors(custom_resolution)
                        if resolution_errors:
                            row_failed = True
                            reason = f"{tpl.filename}: " + "；".join(resolution_errors)
                            failures.append((row.row_no, row.sku, reason))
                            add_render_record(
                                render_records,
                                row.row_no,
                                row.sku,
                                tpl.filename,
                                name,
                                "失败",
                                resolution_errors,
                            )
                            log(f"[{i}] ✗ SKU {row.sku} · {reason}", error=True)
                            continue
                        notes = [*row_notes] if aluminum else [
                            *row_notes,
                            *group_default_notes(tpl, row),
                            *custom_resolution_notes(custom_resolution),
                        ]
                        for note in notes[len(row_notes):]:
                            log(f"[{i}] ⚠ SKU {row.sku} · {tpl.filename}: {note}", error=True)
                        try:
                            if aluminum:
                                unified_plan = order_pipeline.compile_plan(
                                    tpl, row, aluminum_plan_cfg(tpl), template_dir_field.value, strict=False,
                                )
                                if not unified_plan.get("ok"):
                                    raise ValueError("；".join(unified_plan.get("errors") or ["统一订单计划失败"]))
                                for warning_text in [
                                    *(unified_plan.get("warnings") or []),
                                    *order_analysis.special_request_warnings(order_pipeline.parse_fields(row.custom_info)),
                                ]:
                                    notes.append(warning_text)
                                    log(f"[{i}] ⚠ SKU {row.sku} · {tpl.filename}: {warning_text}", error=True)
                                visibility_actions = unified_plan["visibilityActions"]
                                all_path_replacements = unified_plan["pathReplacements"]
                                smart_object_text_replacements = unified_plan["smartObjectTextReplacements"]
                                image_replacements = unified_plan["imageReplacements"]
                            else:
                                visibility_actions, path_replacements = build_group_render_params(tpl, row)
                                custom_main_replacements, _ = split_custom_replacements(
                                    custom_resolution.replacements,
                                )
                                all_path_replacements = custom_fields.merge_path_replacements(
                                    build_default_path_replacements(tpl, row, vf),
                                    path_replacements,
                                    custom_main_replacements,
                                )
                                smart_object_text_replacements = build_smart_object_render_params(
                                    tpl, row, vf, custom_resolution.replacements,
                                )
                                image_replacements = build_image_render_params(tpl, row)
                            target_dir = output_layout.row_dir(root_dir, row, category, run_day)
                            os.makedirs(target_dir, exist_ok=True)
                            result = renderer.render(
                                tpl.path, {},
                                os.path.join(target_dir, name),
                                visibility_actions=visibility_actions,
                                path_replacements=all_path_replacements,
                                smart_object_text_replacements=smart_object_text_replacements,
                                image_replacements=image_replacements,
                                # Keep each PSD text layer's original font. Photoshop may
                                # substitute a missing font while editing, but the render
                                # flow must not force a bundled font onto the order.
                                font_name="",
                                log=lambda m, i=i: log(f"[{i}] {m}", error=True),
                            )
                            success_images += 1
                            if result.get("photoshopStartupSeconds", 0) >= 10:
                                log(f"[{i}] Photoshop 启动用时 {result['photoshopStartupSeconds']:.0f} 秒")
                            name = os.path.relpath(os.path.join(target_dir, name), out_dir)
                            log(f"[{i}] SKU {row.sku} · {tpl.filename} → {name}"
                                f"（{result.get('elapsedSeconds', 0):.0f} 秒）")
                            for warning_text in result.get("fontWarnings", []):
                                log(f"[{i}] ⚠ 字体提示: {warning_text}", error=True)
                            for fitted_text in result.get("textFitted", []):
                                log(f"[{i}] ⚠ 长文字已缩小: {fitted_text}", error=True)
                            for fitted_text in result.get("textBoxFitted", []):
                                log(f"[{i}] 文字{fitted_text}")
                            for image_text in result.get("imageReplaced", []):
                                log(f"[{i}] 图片替换: {image_text}")
                            add_render_record(
                                render_records,
                                row.row_no,
                                row.sku,
                                tpl.filename,
                                name,
                                "成功",
                                [
                                    *notes,
                                    *result.get("fontWarnings", []),
                                    *result.get("textFitted", []),
                                    *result.get("textBoxFitted", []),
                                    *[
                                        "图片替换: " + text
                                        for text in result.get("imageReplaced", [])
                                    ],
                                ],
                            )
                        except Exception as e:  # noqa: BLE001
                            row_failed = True
                            reason = f"{tpl.filename}: {e}"
                            failures.append((row.row_no, row.sku, reason))
                            add_render_record(
                                render_records,
                                row.row_no,
                                row.sku,
                                tpl.filename,
                                name,
                                "失败",
                                [*notes, str(e)],
                            )
                            log(f"[{i}] ✗ SKU {row.sku} · {tpl.filename} 失败: {e}", error=True)
                    if row_failed:
                        failed_rows += 1
                    else:
                        generated_rows.add(row.row_no)
                progress.value = i / total if total else 1
                progress_text.value = f"{i} / {total} 行 · 成功 {success_images} 张 · 失败 {failed_rows} 行"
                page.update()

            report_path = None
            failures.sort(key=lambda item: item[0])
            # Always rewrite the report so a previous run's failures never
            # linger next to a fully successful re-run.
            failure_report = os.path.join(out_dir, f"失败报告-{category}.csv")
            with open(failure_report, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["行号", "SKU", "原因"])
                writer.writerows(failures)
            if failures:
                report_path = failure_report
            log_path = write_render_log(out_dir, render_records, category)
            task_title.value = "生成完成"
            show_summary(success_images, failed_rows, report_path, log_path, skipped_rows)
        except Exception:  # noqa: BLE001
            log("发生未预期错误:\n" + traceback.format_exc(), error=True)
            try:
                log_path = write_render_log(out_dir, render_records, category)
                log(f"已写入生成日志: {log_path}")
            except Exception as e:  # noqa: BLE001
                log(f"写入生成日志失败: {e}", error=True)
            task_title.value = "任务异常中止"
        finally:
            mark_generated_rows(generated_rows)
            state["running"] = False
            back_btn.disabled = False
            page.update()

    def start_task():
        excel = state["excel"]
        os.makedirs(output_dir_field.value, exist_ok=True)
        name_column = str(output_name_column_field.value or "").strip()
        mode = (
            f"自定义命名列: {name_column}" if name_column
            else "hash 命名模式" if excel.hash_mode
            else "交易编号命名模式" if excel.order_mode
            else "普通命名模式"
        )
        task_title.value = "正在生成…"
        row_count = f"共 {len(excel.rows)} 行"
        if selected_row_numbers():
            row_count = f"共 {len(excel.rows)} 行，只生成勾选的 {len(selected_row_numbers())} 行"
        elif is_aluminum_mode():
            ready = sum(1 for row in excel.rows
                        if (order := normalized_order_for(row)) is not None and order.status == "ready")
            row_count = f"共 {len(excel.rows)} 行，生成其中可生成的 {ready} 行"
        task_subtitle.value = (
            f"{os.path.basename(state['excel_path'])} · {row_count}"
            f"（{mode}）→ {output_layout.day_dir(output_dir_field.value)}"
        )
        log_list.controls.clear()
        progress.value = 0
        progress_text.value = ""
        state["running"] = True
        back_btn.disabled = True
        show_task_page()
        threading.Thread(target=run_render, daemon=True).start()

    # ---------- 启动 ----------
    show_config_page()
    if template_dir_field.value:
        rescan()


if __name__ == "__main__":
    import single_instance

    _instance_lock = single_instance.acquire()
    if _instance_lock is None:
        single_instance.notify_already_running()
        sys.exit(0)
    ft.app(main)
