#!/bin/bash
# ============================================================
# verysync (微力同步) 安装 - 兼容入口
#
# verysync 官方不提供可直接抓取的稳定下载链接, 需从官网手动获取:
#   https://www.verysync.com/download
#
# 本文件保留仅为兼容旧路径, 安装指引统一由 scripts/install-services.sh
# 输出 (避免两处说明各自漂移)。仍会创建本地数据目录。
# ============================================================
set -euo pipefail

VSYNC_DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$VSYNC_DIR/temp"

TARGET="$(cd "$(dirname "$0")/../../scripts" && pwd)/install-services.sh"
if [ ! -f "$TARGET" ]; then
    echo "[*] verysync 需手动安装: https://www.verysync.com/download (Linux ARM 版)"
    exit 0
fi

exec "$TARGET" verysync
