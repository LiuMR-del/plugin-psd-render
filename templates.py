"""模版目录扫描与索引.

目录约定:
    模版目录/
    ├── 51052004/                    <- 目录名 = SKU
    │   ├── 父亲节卡片.psd            <- 默认模版 (field_value = "")
    │   ├── 父亲节卡片-[红色].psd     <- 变体模版 (field_value = "红色")
    │   └── 父亲节卡片-[蓝色].psd
    └── 51052005/
        └── 贺卡.psd

同时支持 .psd 与 .psb(大文档格式, 文件 >2GB 或画布 >30000px 时 Photoshop 存为 PSB);
psd-tools 与 Photoshop 均原生识别两种格式, 处理方式完全一致。

PSD 图层解析用 psd-tools 只读打开(不 composite), 收集完整图层清单与「未隐藏」的文字图层;
同时为每个模版生成缩略图。解析结果与缩略图都缓存在模版目录下
(.psd-render-cache.json / .psd-render-thumbs/), 逐文件比对 mtime+size,
命中则跳过解析。缓存随模版目录走, 换机器 / 多人共享目录也能复用。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field

import order_intake

CACHE_FILENAME = ".psd-render-cache.json"
THUMB_DIRNAME = ".psd-render-thumbs"
CACHE_VERSION = 9  # v9: 文字图层实际使用的字体（开跑前查缺字体）；v8: 图层 bbox 与文字内容
THUMB_MAX = 480

_ILLEGAL_FILENAME_CHARS = '\\/:*?"<>|'
_TEMPLATE_EXTENSIONS = (".psd", ".psb")
_SIZE_IN_FILENAME_RE = re.compile(
    r"[（(]\s*(?P<left>\d+(?:[.,]\d+)?)\s*[-—–－x×*]\s*"
    r"(?P<right>\d+(?:[.,]\d+)?)\s*(?:cm)?\s*[）)]",
    re.IGNORECASE,
)
_VARIANT_RE = re.compile(r"^(?P<name>.+?)-\[(?P<value>[^]]+)\]$")



@dataclass
class Template:
    sku: str
    name: str          # 模版名(不含变体后缀与扩展名)
    field_value: str   # 模版变体字段值, "" 表示默认模版
    path: str
    filename: str
    text_layers: list[str] = field(default_factory=list)  # 未隐藏的文字图层名(去重保序)
    all_layers: list[dict] = field(default_factory=list)  # PSD 完整图层清单(含组/隐藏/非文字)
    width: int = 0
    height: int = 0
    thumb: str = ""    # 缩略图绝对路径, "" 表示生成失败
    size_key: str = ""  # 文件名/档案解析出的严格方向尺寸, 例如 30x20
    description: str = ""  # 尺寸括号后的附加说明
    structure_signature: str = ""  # 稳定图层结构签名


def parse_template_filename(filename: str, fallback_sku: str = "") -> tuple[str, str, str, str]:
    """解析平铺模板名，返回 (sku, size_key, description, variant_value)。"""
    stem = os.path.splitext(os.path.basename(filename))[0]
    variant_value = ""
    variant_match = _VARIANT_RE.match(stem)
    if variant_match:
        stem = variant_match.group("name")
        variant_value = variant_match.group("value").strip()
    size_match = _SIZE_IN_FILENAME_RE.search(stem)
    if not size_match:
        return (str(fallback_sku or stem).strip(), "", stem, variant_value)
    sku = stem[:size_match.start()].strip().rstrip("-_ ") or str(fallback_sku or "").strip()
    size_key = order_intake.dimension_key_from_cm(
        size_match.group("left"), size_match.group("right"),
    )
    description = stem[size_match.end():].strip(" _-—–")
    return sku, size_key, description, variant_value


def parse_filename(filename: str) -> tuple[str, str]:
    """从文件名解析 (模版名, 变体字段值)。

    '父亲节卡片-[红色].psd' -> ('父亲节卡片', '红色')
    '父亲节卡片.psd'        -> ('父亲节卡片', '')
    """
    stem = filename[: filename.rfind(".")]
    if stem.endswith("]"):
        marker = stem.rfind("-[")
        if marker > 0:
            return stem[:marker], stem[marker + 2 : -1]
    return stem, ""


def _layer_fonts(layer) -> list[str]:
    """PostScript names of the fonts a text layer's runs actually use.

    ``FontSet`` alone also lists fonts nobody uses (Photoshop's CJK default
    ``AdobeHeitiStd`` sits in almost every file), so each style run's font
    index is resolved; a run without its own font uses the default style sheet.
    """
    try:
        resources = layer.resource_dict
        font_set = [str(item["Name"]).strip("'\x00 ") for item in resources["FontSet"]]
        runs = layer.engine_dict["StyleRun"]["RunArray"]
    except Exception:  # noqa: BLE001 - 读不到字体时不影响其余解析
        return []
    default = None
    try:
        sheets = resources["StyleSheetSet"]
        default = int(sheets[int(resources.get("TheNormalStyleSheet", 0))]["StyleSheetData"]["Font"])
    except Exception:  # noqa: BLE001
        pass
    used: list[str] = []
    for run in runs:
        try:
            data = run["StyleSheet"]["StyleSheetData"]
            index = int(data["Font"]) if "Font" in data else default
        except Exception:  # noqa: BLE001
            index = default
        if index is not None and 0 <= index < len(font_set):
            used.append(font_set[index])
    return [name for name in dict.fromkeys(used) if name and name != "AdobeInvisFont"]


def used_fonts(template) -> dict[str, list[str]]:
    """{font: [layer labels]} for visible text layers, incl. visible editable smart objects."""
    result: dict[str, list[str]] = {}

    def add(layer: dict, prefix: str) -> None:
        if layer.get("kind") != "type" or not layer.get("visible", True):
            return
        label = prefix + str(layer.get("path") or layer.get("name") or "")
        for font in layer.get("fonts") or []:
            result.setdefault(str(font), []).append(label)

    for layer in getattr(template, "all_layers", []) or []:
        add(layer, "")
        info = layer.get("smart_object") or {}
        if layer.get("kind") == "smartobject" and info.get("editable") and layer.get("visible", True):
            for child in info.get("layers") or []:
                add(child, f"{layer.get('path') or layer.get('name')} › ")
    return result


def _parse_psd(path: str) -> dict:
    """用 psd-tools 只读解析 PSD: 完整图层清单 + 未隐藏文字图层名 + 画布尺寸。"""
    from psd_tools import PSDImage

    psd = PSDImage.open(path)
    text_layers: list[str] = []
    all_layers: list[dict] = []

    def layer_kind(layer) -> str:
        return "group" if layer.is_group() else str(getattr(layer, "kind", "") or "layer")

    def collect_layers(container, include_smart_contents: bool,
                       parent_path: tuple[str, ...] = (),
                       parent_visible: bool = True, depth: int = 0) -> list[dict]:
        collected: list[dict] = []
        for layer in container:
            name = str(layer.name or "")
            visible = bool(getattr(layer, "visible", True))
            effective_visible = parent_visible and visible
            is_group = layer.is_group()
            kind = layer_kind(layer)
            layer_path = (*parent_path, name)
            item = {
                "name": name,
                "path": "/".join(layer_path),
                "path_parts": list(layer_path),
                "kind": kind,
                "visible": effective_visible,
                "depth": depth,
            }
            try:
                # psd-tools returns (left, top, right, bottom).
                left, top, right, bottom = layer.bbox
                item["bbox"] = [int(left), int(top), int(right), int(bottom)]
            except (AttributeError, TypeError, ValueError):
                pass
            if kind == "type":
                try:
                    item["text"] = str(layer.text or "")[:200]
                except Exception:  # noqa: BLE001 - 文字内容只用于识别年份/姓名
                    pass
                item["fonts"] = _layer_fonts(layer)
            if kind == "smartobject":
                if include_smart_contents:
                    item["smart_object"] = parse_smart_object(layer)
                else:
                    item["smart_object"] = {
                        "editable": False,
                        "reason": "暂不支持嵌套智能对象",
                        "layers": [],
                    }
            collected.append(item)
            if is_group:
                collected.extend(collect_layers(
                    layer, include_smart_contents, layer_path,
                    effective_visible, depth + 1,
                ))
        return collected

    def parse_smart_object(layer) -> dict:
        info = {
            "id": "",
            "source_kind": "",
            "file_type": "",
            "filename": "",
            "editable": False,
            "reason": "无法读取智能对象内容",
            "layers": [],
        }
        try:
            smart = layer.smart_object
            info.update({
                "id": str(smart.unique_id or ""),
                "source_kind": str(smart.kind or ""),
                "file_type": str(smart.filetype or ""),
                "filename": str(smart.filename or ""),
            })
            if smart.kind != "data":
                info["reason"] = "链接型智能对象不支持内部文字编辑"
                return info
            is_psd_content = (
                info["file_type"].lower() in ("8bpb", "8bps", "psd", "psb") or
                info["filename"].lower().endswith((".psd", ".psb"))
            )
            if not is_psd_content:
                label = info["filename"] or info["file_type"] or "当前格式"
                info["reason"] = f"{label} 不是嵌入式 PSD/PSB"
                return info
            with smart.open() as embedded_file:
                embedded_psd = PSDImage.open(embedded_file)
                info["layers"] = collect_layers(
                    embedded_psd, include_smart_contents=False,
                )
            info["editable"] = True
            info["reason"] = ""
        except Exception as exc:  # noqa: BLE001 - 单个对象失败时仍保留模板其余图层
            info["reason"] = f"智能对象内容解析失败: {exc}"
        return info

    all_layers.extend(collect_layers(psd, include_smart_contents=True))
    for layer in all_layers:
        if bool(layer.get("visible", True)) and layer.get("kind") == "type":
            name = str(layer.get("name") or "")
            if name:
                text_layers.append(name)

    return {
        "text_layers": list(dict.fromkeys(text_layers)),
        "all_layers": all_layers,
        "width": psd.width,
        "height": psd.height,
    }


def structure_signature(width: int, height: int, all_layers: list[dict]) -> str:
    """Return a stable hash for the PSD structure used by template profiles."""
    def clean_layer(layer: dict) -> dict:
        smart = dict(layer.get("smart_object") or {})
        inner = [clean_layer(item) for item in smart.get("layers") or []]
        clean = {
            "path_parts": [str(part) for part in layer.get("path_parts") or []],
            "kind": str(layer.get("kind") or ""),
            "visible": bool(layer.get("visible", True)),
        }
        # bbox/text are deliberately excluded: moving or retyping a layer must
        # not invalidate an approved profile (v7 caches never stored bbox).
        if smart:
            clean["smart_object"] = {
                "id": str(smart.get("id") or ""),
                "source_kind": str(smart.get("source_kind") or ""),
                "file_type": str(smart.get("file_type") or ""),
                "filename": str(smart.get("filename") or ""),
                "editable": bool(smart.get("editable")),
                "layers": inner,
            }
        return clean

    payload = {
        "canvas": [int(width or 0), int(height or 0)],
        "layers": [clean_layer(layer) for layer in all_layers],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _make_thumbnail(psd_path: str, thumb_path: str) -> bool:
    """生成模版缩略图。优先用文件内嵌合成图(最大兼容性保存时存在), 避免逐层合成。"""
    try:
        from psd_tools import PSDImage

        psd = PSDImage.open(psd_path)
        image = psd.topil()
        if image is None:
            image = psd.composite()
        if image is None:
            return False
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")
        image.thumbnail((THUMB_MAX, THUMB_MAX))
        os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
        image.save(thumb_path, "PNG")
        return True
    except Exception:  # noqa: BLE001
        return False


def _thumb_name(sku: str, relative_path: str) -> str:
    """Build a collision-free thumbnail name for templates at any depth."""
    stem = os.path.splitext(relative_path)[0].replace("\\", "/")
    digest = hashlib.sha1(stem.encode("utf-8")).hexdigest()[:10]
    raw = f"{sku}__{stem}__{digest}.png"
    return "".join("_" if c in _ILLEGAL_FILENAME_CHARS else c for c in raw)


def _load_cache(template_dir: str) -> dict:
    cache_path = os.path.join(template_dir, CACHE_FILENAME)
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") == CACHE_VERSION and isinstance(data.get("entries"), dict):
            return data["entries"]
    except (OSError, ValueError):
        pass
    return {}


def _save_cache(template_dir: str, entries: dict) -> None:
    cache_path = os.path.join(template_dir, CACHE_FILENAME)
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"version": CACHE_VERSION, "entries": entries}, f, ensure_ascii=False, indent=1)
    except OSError:
        pass  # 只读目录等场景下缓存写失败不影响功能


def _prune_thumbnails(thumb_dir: str, keep: set[str]) -> None:
    """Drop thumbnails of templates that are gone or renamed (like stale cache entries)."""
    try:
        names = os.listdir(thumb_dir)
    except OSError:
        return
    for name in names:
        path = os.path.join(thumb_dir, name)
        if name.lower().endswith(".png") and name not in keep and os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass  # 删不掉不影响功能


def scan_templates(template_dir: str, force: bool = False) -> tuple[dict[str, list[Template]], dict]:
    """扫描旧目录和根目录平铺模板，返回 SKU 索引和统计信息。"""
    old_cache = {} if force else _load_cache(template_dir)
    new_cache: dict = {}
    index: dict[str, list[Template]] = {}
    cache_hits = 0
    errors: list[tuple[str, str]] = []
    flat_count = 0
    thumb_dir = os.path.join(template_dir, THUMB_DIRNAME)

    candidates: list[tuple[str, str, str]] = []
    walk_errors: list[tuple[str, str]] = []

    def on_walk_error(exc: OSError) -> None:
        walk_errors.append((os.path.relpath(str(exc.filename or template_dir), template_dir), f"无法读取目录: {exc}"))

    try:
        for current, dirs, files in os.walk(template_dir, onerror=on_walk_error):
            # Tool-owned cache folders never contain source templates and must
            # not be traversed as user template directories.
            dirs[:] = sorted(
                directory for directory in dirs
                if not directory.startswith(".") and directory != THUMB_DIRNAME
            )
            relative_dir = os.path.relpath(current, template_dir)
            fallback_sku = "" if relative_dir == "." else os.path.basename(current)
            for fname in sorted(files):
                if fname.startswith(".") or not fname.lower().endswith(_TEMPLATE_EXTENSIONS):
                    continue
                path = os.path.join(current, fname)
                candidates.append((fallback_sku, fname, path))
    except OSError as exc:
        return {}, {"sku_count": 0, "template_count": 0, "variant_count": 0,
                    "cache_hits": 0, "flat_count": 0, "errors": [(".", str(exc))]}

    for fallback_sku, fname, path in candidates:
        relative_path = os.path.relpath(path, template_dir).replace(os.sep, "/")
        if "/" not in relative_path:
            flat_count += 1
        parsed_sku, size_key, description, parsed_variant = parse_template_filename(
            fname, fallback_sku=fallback_sku,
        )
        sku = parsed_sku or fallback_sku
        if not sku:
            errors.append((relative_path, "无法从模板文件名或目录名识别 SKU"))
            continue
        key = relative_path
        stat = os.stat(path)
        cached = old_cache.get(key)
        thumb_path = os.path.join(thumb_dir, _thumb_name(sku, relative_path))
        if cached and cached.get("mtime") == stat.st_mtime and cached.get("size") == stat.st_size:
            info = dict(cached)
            cache_hits += 1
            if info.get("thumb") and not os.path.exists(thumb_path):
                if _make_thumbnail(path, thumb_path):
                    info["thumb"] = _thumb_name(sku, relative_path)
                else:
                    info["thumb"] = ""
        else:
            try:
                parsed = _parse_psd(path)
                parsed["structure_signature"] = structure_signature(
                    parsed.get("width", 0), parsed.get("height", 0),
                    parsed.get("all_layers") or [],
                )
            except Exception as exc:  # noqa: BLE001 - 单个坏文件不中断扫描
                errors.append((key, str(exc)))
                parsed = {
                    "text_layers": [], "all_layers": [], "width": 0, "height": 0,
                    "structure_signature": "",
                }
            thumb_ok = _make_thumbnail(path, thumb_path)
            info = {
                "mtime": stat.st_mtime, "size": stat.st_size,
                "thumb": _thumb_name(sku, relative_path) if thumb_ok else "",
                **parsed,
            }
        # 旧缓存可能没有 v7 结构签名，补算并在下次保存时固化。
        if not info.get("structure_signature"):
            info["structure_signature"] = structure_signature(
                info.get("width", 0), info.get("height", 0), info.get("all_layers") or [],
            )
        new_cache[key] = info
        legacy_name, legacy_variant = parse_filename(fname)
        field_value = parsed_variant or legacy_variant
        name = parsed_sku if size_key else legacy_name
        template = Template(
            sku=sku, name=name, field_value=field_value, path=path, filename=fname,
            text_layers=list(info.get("text_layers") or []),
            all_layers=list(info.get("all_layers") or []),
            width=int(info.get("width") or 0), height=int(info.get("height") or 0),
            thumb=thumb_path if info.get("thumb") else "",
            size_key=size_key,
            description=description if size_key else "",
            structure_signature=str(info.get("structure_signature") or ""),
        )
        index.setdefault(sku, []).append(template)

    for templates_for_sku in index.values():
        templates_for_sku.sort(key=lambda item: (item.size_key, item.field_value, item.filename))
    _save_cache(template_dir, new_cache)
    # 有子目录没读到时不知道哪些模板还在, 不删缩略图
    if not walk_errors:
        _prune_thumbnails(thumb_dir, {os.path.basename(t.thumb) for v in index.values() for t in v if t.thumb})
    template_count = sum(len(v) for v in index.values())
    variant_count = sum(1 for v in index.values() for t in v if t.field_value)
    stats = {
        "sku_count": len(index),
        "template_count": template_count,
        "variant_count": variant_count,
        "cache_hits": cache_hits,
        "flat_count": flat_count,
        "errors": walk_errors + errors,
        "complete": not walk_errors,  # 每个子目录都读到了
    }
    return index, stats


def pick_by_size(index: dict[str, list[Template]], sku: str, size_key: str,
                 field_value: str = "") -> list[Template]:
    """Strictly pick templates by SKU + orientation-preserving size."""
    candidates = [item for item in index.get(sku, []) if item.size_key == str(size_key or "")]
    if field_value:
        exact = [item for item in candidates if item.field_value == field_value]
        if exact:
            return exact
    return [item for item in candidates if item.field_value == ""]


def pick(index: dict[str, list[Template]], sku: str, field_value: str = "") -> list[Template]:
    """挑选某行订单要渲染的模版列表。

    优先 field_value 精确匹配变体模版; 无匹配回退默认模版(field_value == "")。
    sku 不存在返回 []。
    """
    candidates = index.get(sku)
    if not candidates:
        return []
    if field_value:
        exact = [t for t in candidates if t.field_value == field_value]
        if exact:
            return exact
    return [t for t in candidates if t.field_value == ""]
