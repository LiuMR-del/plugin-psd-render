"""模板库「清理无效记录」: 先列出可清理项, 用户确认后再删.

只删程序自己生成的记录, 不碰 PSD/PSB、字体、订单 Excel 和输出图片:
- 模板档案 (.psd-render-profiles.json) 里已找不到对应模板的条目
- config.json 里按模板库记的界面配置: 目录已不存在的库, 以及用户勾选的其他模板库
PSD 解析缓存和缩略图每次扫描只保留现有模板的, 不需要在这里清理.
"""

from __future__ import annotations

import os
from typing import Iterable

import settings
import template_profiles


def _same_dir(left: str, right: str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def find_stale(template_dir: str, index: dict, store: dict, cfg: dict) -> dict:
    """List what can be cleaned for the freshly scanned ``index`` of ``template_dir``.

    Returns {profiles: [(key, label)], missing_libraries: [dir], other_libraries: [dir]}.
    """
    used = {
        template_profiles.matched_profile_key(store, t.sku, t.size_key, t.filename, t.path)
        for group in (index or {}).values() for t in group
    }
    profiles = []
    for key, profile in sorted((store.get("profiles") or {}).items()):
        if key in used:
            continue
        profile = profile if isinstance(profile, dict) else {}
        label = str(profile.get("template_filename") or key)
        status = str(profile.get("status") or "")
        profiles.append((key, f"{label}（{status}）" if status else label))

    libraries = sorted({directory for key in settings.PER_LIBRARY_KEYS
                        for directory in (cfg.get(key) or {})})
    missing = [d for d in libraries if not os.path.isdir(d)]
    others = [d for d in libraries if os.path.isdir(d) and not _same_dir(d, template_dir)]
    return {"profiles": profiles, "missing_libraries": missing, "other_libraries": others}


def apply(template_dir: str, store: dict, cfg: dict, *, profile_keys: Iterable[str] = (),
          libraries: Iterable[str] = ()) -> dict:
    """Delete the confirmed items in place (caller saves ``store``/``cfg``); return counts."""
    profiles = store.get("profiles") or {}
    removed = {"profiles": 0, "libraries": 0}
    for key in profile_keys:
        if profiles.pop(key, None) is not None:
            removed["profiles"] += 1
    for directory in libraries:
        if _same_dir(directory, template_dir):
            continue  # never drop the library being worked on
        dropped = [(cfg.get(key) or {}).pop(directory, None) for key in settings.PER_LIBRARY_KEYS]
        removed["libraries"] += any(item is not None for item in dropped)
    return removed
