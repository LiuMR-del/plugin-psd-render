#!/bin/bash
# PSD 批量生图 — macOS/Linux 一键打包源码 ZIP
# 输出: dist/plugin-psd-render.zip
# 用 Python zipfile 打包: 它给中文文件名打 UTF-8 标记; macOS 自带的 zip 不打,
# Windows 资源管理器解压后中文文件名(使用说明.txt、Windows自检.bat 等)会乱码。

cd "$(dirname "$0")" || exit 1

PYTHON=".venv/bin/python"
[ -x "$PYTHON" ] || PYTHON=$(command -v python3)
[ -n "$PYTHON" ] || { echo "未找到 Python, 请先运行一次 start.sh"; read -r -p "按回车退出..."; exit 1; }

"$PYTHON" - <<'EOF' || { read -r -p "打包失败, 按回车退出..."; exit 1; }
import fnmatch
import os
import zipfile

OUT = os.path.join("dist", "plugin-psd-render.zip")
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pycache", "build", "dist", "node_modules", "自检结果",
             ".ruff_cache", ".zcode"}
SKIP_FILES = (".DS_Store", ".env", ".env.*", "*.pyc", "*.spec")

os.makedirs("dist", exist_ok=True)
if os.path.exists(OUT):
    os.remove(OUT)
count = 0
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as archive:
    for current, dirs, files in os.walk("."):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            if any(fnmatch.fnmatch(name, pattern) for pattern in SKIP_FILES):
                continue
            path = os.path.join(current, name)
            archive.write(path, os.path.relpath(path, "."))
            count += 1
print(f"打包完成: {OUT}（{count} 个文件）")
EOF
