#!/bin/bash
# PSD 批量生图 — macOS/Linux 一键启动: bash start.sh (或 ./start.sh, macOS 也可双击 启动.command)
# 每次启动自动检查并修复运行环境:
#   1. 找 Python 3.10+; 没有时询问后用 uv 安装到用户目录(不需要管理员权限)
#   2. .venv 不存在或已损坏(如从别的电脑拷来)时重建
#   3. 依赖缺失或 requirements.txt 有变化时重装; 官方源失败时换国内镜像重试
#   4. 检查 Photoshop, 没装只提示(仍可打开程序审核模板)
# 带参数 selfcheck 时环境就绪后运行 windows_selfcheck.py(两个平台通用的自检), 而不是打开程序

cd "$(dirname "$0")" || exit 1

VENV_PY=".venv/bin/python"
STAMP=".venv/.requirements.sha256"
PYPI_MIRRORS=("" "https://pypi.tuna.tsinghua.edu.cn/simple" "https://mirrors.aliyun.com/pypi/simple/")

fail() {
    echo "$1"
    read -r -p "按回车退出..."
    exit 1
}

python_ok() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

# 输出可用的 Python 3.10+ 路径
find_python() {
    local candidate path
    for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
        path=$(command -v "$candidate" 2>/dev/null) || continue
        # macOS 没装命令行工具时, /usr/bin/python3 一运行就弹安装窗口: 跳过
        if [ "$path" = "/usr/bin/python3" ] && [ "$(uname)" = "Darwin" ] && ! xcode-select -p >/dev/null 2>&1; then
            continue
        fi
        if python_ok "$path"; then
            echo "$path"
            return 0
        fi
    done
    local uv_bin
    uv_bin=$(command -v uv 2>/dev/null || echo "$HOME/.local/bin/uv")
    if [ -x "$uv_bin" ]; then
        path=$("$uv_bin" python find --system '>=3.10' 2>/dev/null) && python_ok "$path" && { echo "$path"; return 0; }
    fi
    return 1
}

install_python() {
    echo "未找到 Python 3.10+。"
    read -r -p "是否自动下载安装 Python 3.11 到当前用户目录(约 50MB, 不需要管理员权限)? [Y/n] " answer
    case "$answer" in
        [nN]*) fail "请手动安装 Python 3.10+ (https://www.python.org/downloads/) 后再启动。" ;;
    esac
    local uv_bin
    uv_bin=$(command -v uv 2>/dev/null || echo "$HOME/.local/bin/uv")
    if [ ! -x "$uv_bin" ]; then
        echo "下载 Python 安装工具 uv (https://astral.sh/uv)..."
        command -v curl >/dev/null 2>&1 || fail "未找到 curl, 请手动安装 Python 3.10+ (https://www.python.org/downloads/)。"
        # 只装到 ~/.local/bin, 不改 shell 配置文件
        curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh \
            || fail "uv 下载失败(请检查网络), 或手动安装 Python 3.10+ (https://www.python.org/downloads/)。"
        uv_bin="$HOME/.local/bin/uv"
    fi
    echo "安装 Python 3.11..."
    "$uv_bin" python install 3.11 || fail "Python 安装失败(请检查网络), 或手动安装 Python 3.10+ (https://www.python.org/downloads/)。"
}

pip_install() {
    local index
    for index in "${PYPI_MIRRORS[@]}"; do
        if [ -z "$index" ]; then
            echo "安装/更新依赖(需联网)..."
            "$VENV_PY" -m pip install --disable-pip-version-check --retries 2 --timeout 30 -r requirements.txt && return 0
        else
            echo "改用镜像 $index 重试..."
            "$VENV_PY" -m pip install --disable-pip-version-check --retries 2 --timeout 30 -i "$index" -r requirements.txt && return 0
        fi
    done
    return 1
}

requirements_hash() {
    "$VENV_PY" -c "import hashlib; print(hashlib.sha256(open('requirements.txt', 'rb').read()).hexdigest())"
}

check_photoshop() {
    [ "$(uname)" = "Darwin" ] || return 0
    if ls -d /Applications/Adobe\ Photoshop*/Adobe\ Photoshop*.app >/dev/null 2>&1; then
        return 0
    fi
    if [ -n "$(mdfind "kMDItemCFBundleIdentifier == 'com.adobe.Photoshop'" 2>/dev/null | head -1)" ]; then
        return 0
    fi
    echo "提示: 未检测到 Photoshop。程序可以打开、审核模板, 但出图需要安装 Photoshop CC 2018 或更新版本。"
}

# ---- 1-2. Python 与虚拟环境 ----
if [ -d .venv ] && ! "$VENV_PY" -c 'import sys' >/dev/null 2>&1; then
    echo "虚拟环境不完整或已损坏(可能是上次创建被中断, 或从别的电脑拷来), 重新创建..."
    rm -rf .venv
fi
if [ ! -x "$VENV_PY" ]; then
    PYTHON=$(find_python) || { install_python; PYTHON=$(find_python) || fail "安装后仍找不到 Python 3.10+, 请手动安装 (https://www.python.org/downloads/)。"; }
    echo "首次运行: 用 $PYTHON 创建虚拟环境..."
    "$PYTHON" -m venv .venv || fail "创建虚拟环境失败。"
fi

# ---- 3. 依赖: 能导入且 requirements.txt 没变就跳过 ----
deps_ok() {
    "$VENV_PY" -c "import flet, psd_tools, openpyxl, dotenv" >/dev/null 2>&1
}
if [ "$(cat "$STAMP" 2>/dev/null)" != "$(requirements_hash)" ] || ! deps_ok; then
    if pip_install && deps_ok; then
        requirements_hash > "$STAMP"
    elif deps_ok; then
        # 离线时更新失败但旧依赖还能用: 先照常启动, 下次联网再更新
        echo "提示: 依赖更新失败(可能没联网), 先用已安装的版本启动。"
    else
        fail "依赖安装失败, 请检查网络后重试; 仍不行请截图本窗口联系维护人员。"
    fi
fi

# ---- 4. Photoshop(只提示) ----
check_photoshop

if [ "$1" = "selfcheck" ]; then
    "$VENV_PY" windows_selfcheck.py
    read -r -p "按回车退出..."
    exit 0
fi
exec "$VENV_PY" main.py
