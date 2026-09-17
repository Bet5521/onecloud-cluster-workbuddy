#!/bin/bash
# ============================================================
# mihomo (Clash Meta) 安装 - 兼容入口
#
# 实际安装逻辑已统一到 scripts/install-services.sh:
#   - 自动识别 CPU 架构 (armv7 / arm64 / amd64), 不再写死 armv7
#   - 下载后校验产物是否为可执行文件, 避免 404 页面被当成安装成功
#   - 需要 root 权限时给出明确提示, 而非静默失败
#
# 本文件保留仅为兼容旧路径, 不重复实现。
# ============================================================
set -euo pipefail

TARGET="$(cd "$(dirname "$0")/../../scripts" && pwd)/install-services.sh"
if [ ! -f "$TARGET" ]; then
    echo "[ERROR] 未找到统一安装脚本: $TARGET" >&2
    echo "        请先确认 scripts/install-services.sh 已分发到 /mnt/sd/scripts" >&2
    exit 1
fi

exec "$TARGET" mihomo
