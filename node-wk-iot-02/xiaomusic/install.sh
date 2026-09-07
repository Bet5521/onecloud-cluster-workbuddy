#!/bin/bash
# ============================================================
# xiaomusic 安装 - 兼容入口
#
# 实际安装逻辑已统一到 scripts/install-services.sh:
#   - 自动识别 CPU 架构 (armv7 / arm64 / amd64), 不再按 "arm" 模糊匹配
#     (旧实现可能在本应是 armv7 的设备上误装 arm64 构建)
#   - 下载后校验产物是否为可执行文件, 避免 404 页面被当成安装成功
#   - 需要 root 权限时给出明确提示
#
# 本文件保留仅为兼容旧路径: 仍会创建本地数据目录, 然后转交统一安装脚本。
# ============================================================
set -euo pipefail

XMUSIC_DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$XMUSIC_DIR/downloads" "$XMUSIC_DIR/cache" "$XMUSIC_DIR/session"

TARGET="$(cd "$(dirname "$0")/../../scripts" && pwd)/install-services.sh"
if [ ! -f "$TARGET" ]; then
    echo "[ERROR] 未找到统一安装脚本: $TARGET" >&2
    echo "        请先确认 scripts/install-services.sh 已分发到 /mnt/sd/scripts" >&2
    exit 1
fi

exec "$TARGET" xiaomusic
