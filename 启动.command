#!/bin/bash
# PSD 批量生图 — macOS 双击启动（在访达中双击本文件）
# 实际启动逻辑在 start.sh：首次运行自动创建虚拟环境并安装依赖

cd "$(dirname "$0")" || exit 1
exec bash ./start.sh
