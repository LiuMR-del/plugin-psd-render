"""Install fonts shipped inside the template library for the current user.

Photoshop only sees system-installed fonts; a font file merely lying in the
template library is ignored and Photoshop silently substitutes another font.
On every template-library scan, ``install_library_fonts`` finds
``.otf/.ttf/.ttc/.otc`` files in the library, compares their PostScript names
with fonts already installed (system, user, Adobe Fonts) and copies only the
missing ones into the per-user font folder (no administrator rights needed).
Nothing is ever deleted or overwritten.
"""
from __future__ import annotations

import os
import shutil
import struct
import sys

import templates as templates_module

FONT_EXTENSIONS = (".otf", ".ttf", ".ttc", ".otc")
_SKIP_DIRS = {".psd-render-thumbs"}


def find_library_fonts(template_dir: str) -> list[str]:
    result: list[str] = []
    for current, dirs, files in os.walk(template_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS)
        for name in sorted(files):
            if not name.startswith(".") and name.lower().endswith(FONT_EXTENSIONS):
                result.append(os.path.join(current, name))
    return result


def _read_name_table(handle, offset: int) -> set[str]:
    handle.seek(offset)
    header = handle.read(12)
    if len(header) < 12:
        return set()
    num_tables = struct.unpack(">H", header[4:6])[0]
    records = handle.read(16 * num_tables)
    name_offset = None
    for index in range(num_tables):
        tag, _, table_offset, _ = struct.unpack(">4sIII", records[index * 16:(index + 1) * 16])
        if tag == b"name":
            name_offset = table_offset
            break
    if name_offset is None:
        return set()
    handle.seek(name_offset)
    _, count, string_offset = struct.unpack(">HHH", handle.read(6))
    entries = handle.read(12 * count)
    names: set[str] = set()
    for index in range(count):
        platform, encoding, _, name_id, length, value_offset = struct.unpack(
            ">HHHHHH", entries[index * 12:(index + 1) * 12],
        )
        if name_id not in (4, 6):  # full name, PostScript name
            continue
        handle.seek(name_offset + string_offset + value_offset)
        raw = handle.read(length)
        try:
            text = raw.decode("utf-16-be") if platform in (0, 3) else raw.decode("latin-1")
        except UnicodeDecodeError:
            continue
        text = text.strip().casefold()
        if text:
            names.add(text)
    return names


def font_names(path: str) -> set[str]:
    """PostScript/full names of every face in a font file; empty when unreadable."""
    try:
        with open(path, "rb") as handle:
            tag = handle.read(4)
            if tag == b"ttcf":
                handle.seek(8)
                count = struct.unpack(">I", handle.read(4))[0]
                offsets = struct.unpack(f">{count}I", handle.read(4 * count))
            else:
                offsets = (0,)
            names: set[str] = set()
            for offset in offsets:
                names |= _read_name_table(handle, offset)
            return names
    except (OSError, struct.error, ValueError):
        return set()


def user_font_dir() -> str:
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Fonts")
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return os.path.join(base, "Microsoft", "Windows", "Fonts")
    return os.path.expanduser("~/.local/share/fonts")


def installed_font_dirs() -> list[str]:
    if sys.platform == "darwin":
        dirs = [
            "~/Library/Fonts", "/Library/Fonts", "/System/Library/Fonts",
            "/System/Library/Fonts/Supplemental",
            # Adobe Fonts activated in Creative Cloud are usable by Photoshop.
            "~/Library/Application Support/Adobe/CoreSync/plugins/livetype",
            # Adobe common fonts folder, also seen by Photoshop.
            "/Library/Application Support/Adobe/Fonts",
        ]
    elif sys.platform == "win32":
        dirs = [os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts"), user_font_dir(),
                os.path.join(os.environ.get("APPDATA", ""), "Adobe", "CoreSync", "plugins", "livetype"),
                os.path.join(os.environ.get("CommonProgramFiles", "C:\\Program Files\\Common Files"),
                             "Adobe", "Fonts")]
    else:
        dirs = ["~/.local/share/fonts", "~/.fonts", "/usr/share/fonts", "/usr/local/share/fonts"]
    return [os.path.expanduser(item) for item in dirs if item]


def installed_font_names() -> set[str]:
    names: set[str] = set()
    for directory in installed_font_dirs():
        if not os.path.isdir(directory):
            continue
        for current, _, files in os.walk(directory):
            for name in files:
                if name.lower().endswith(FONT_EXTENSIONS):
                    names |= font_names(os.path.join(current, name))
    return names


USER_FONT_REGISTRY_KEY = r"Software\Microsoft\Windows NT\CurrentVersion\Fonts"


def _register_windows_font(path: str) -> None:
    """Per-user registration so the font survives a reboot (Windows 10 1809+)."""
    import ctypes
    import winreg  # type: ignore[import-not-found]

    # CreateKeyEx opens the key, or creates it on a profile that never had a per-user font.
    key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, USER_FONT_REGISTRY_KEY, 0, winreg.KEY_SET_VALUE)
    try:
        label = os.path.splitext(os.path.basename(path))[0] + (
            " (TrueType)" if path.lower().endswith((".ttf", ".ttc")) else " (OpenType)"
        )
        winreg.SetValueEx(key, label, 0, winreg.REG_SZ, path)
    finally:
        winreg.CloseKey(key)
    ctypes.windll.gdi32.AddFontResourceW(path)
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001D, 0, 0, 0x0002, 1000, None)  # WM_FONTCHANGE


def install_library_fonts(template_dir: str) -> dict:
    """Install missing library fonts; return {installed, present, failed}."""
    report: dict[str, list] = {"installed": [], "present": [], "failed": []}
    fonts = find_library_fonts(template_dir) if template_dir and os.path.isdir(template_dir) else []
    if not fonts:
        return report
    installed = set(installed_font_names())
    target_dir = user_font_dir()
    for path in fonts:
        label = os.path.basename(path)
        names = font_names(path)
        if not names:
            report["failed"].append((label, "无法读取字体名称，文件可能已损坏"))
            continue
        if names & installed:
            report["present"].append(label)
            continue
        try:
            os.makedirs(target_dir, exist_ok=True)
            target = os.path.join(target_dir, label)
            if os.path.exists(target):
                # Never overwrite a different file that happens to share the name.
                stem, ext = os.path.splitext(label)
                target = os.path.join(target_dir, f"{stem}-psd-render{ext}")
                if os.path.exists(target):
                    report["failed"].append((label, f"字体目录已存在同名文件: {target}"))
                    continue
            shutil.copy2(path, target)
            if sys.platform == "win32":
                _register_windows_font(target)
            installed |= names
            report["installed"].append(label)
        except OSError as exc:
            report["failed"].append((label, str(exc)))
    return report


def missing_template_fonts(templates, installed: set[str] | None = None) -> list[dict]:
    """Fonts the given templates use but this computer lacks (2026-10-14 user request).

    Returns ``[{"template", "path", "missing": {font: [layer labels]}}]`` in the
    given order, one entry per template that misses at least one font.  The
    check runs before rendering so the user can add fonts first.
    """
    names = {name.casefold() for name in (installed if installed is not None else installed_font_names())}
    result: list[dict] = []
    seen: set[str] = set()
    for template in templates:
        path = str(getattr(template, "path", "") or "")
        if path in seen:
            continue
        seen.add(path)
        lacking = {font: layers for font, layers in templates_module.used_fonts(template).items()
                   if font.casefold() not in names}
        if lacking:
            result.append({"template": str(getattr(template, "filename", "") or os.path.basename(path)),
                           "path": path, "missing": lacking})
    return result


__all__ = ["find_library_fonts", "font_names", "install_library_fonts", "installed_font_names",
           "missing_template_fonts"]
