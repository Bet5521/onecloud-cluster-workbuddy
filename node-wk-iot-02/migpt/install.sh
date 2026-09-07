#!/bin/bash
# ============================================================
# migpt 安装 - 兼容入口
#
# 本项目采用「方案 3: Flask API 代理」(见本目录 proxy.py),
# 依赖安装逻辑统一到 scripts/install-services.sh:
#   - 兼容 Debian 12+ 的 PEP 668 (externally-managed) 限制
#
# 本文件保留仅为兼容旧路径, 不重复实现。
# ============================================================
set -euo pipefail

MIGPT_DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$MIGPT_DIR"

TARGET="$(cd "$(dirname "$0")/../../scripts" && pwd)/install-services.sh"
if [ ! -f "$TARGET" ]; then
    echo "[*] 请先手动安装依赖: pip3 install flask flask-cors pyyaml requests"
    exit 0
fi

exec "$TARGET" migpt
