#!/bin/bash
# ============================================================
# OneCloud Cluster - SD 卡工具箱 (sd-tools.sh)
#
# 交互式入口: 由用户输入选择要执行的功能。
#   1) 迁移  将无 SD 卡期间安装在 eMMC 的组件迁移到已挂载 SD 卡
#   2) 更换  将 SD 卡内容打包备份到 USB 存储设备(用于更换 SD 卡)
#   3) 格式化 将 SD 卡分区并格式化为 ext4(迁移/更换检测到非 ext4 时也会自动触发)
#
# 也可非交互使用:
#   ./scripts/sd-tools.sh migrate  [参数]   -> 透传 sd-migrate.sh
#   ./scripts/sd-tools.sh replace [参数]   -> 透传 sd-replace.sh
#   ./scripts/sd-tools.sh format   [参数]   -> 透传 sd-format.sh
# ============================================================
set -euo pipefail

# ---------------- 日志 ----------------
log_info()  { echo "[INFO]  $*"; }
log_warn()  { echo "[WARN]  $*" >&2; }
log_error() { echo "[ERROR] $*" >&2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

usage() {
  cat <<'EOF'
SD 卡工具箱
  1) 迁移    将无 SD 卡期间安装在 eMMC 的组件迁移到已挂载 SD 卡
  2) 更换    将 SD 卡内容打包备份到 USB 存储设备(用于更换 SD 卡)
  3) 格式化  将 SD 卡分区并格式化为 ext4
用法:
  $0                 # 交互式菜单
  $0 migrate  [参数] # 直接执行迁移(参数透传给 sd-migrate.sh)
  $0 replace [参数]  # 直接执行更换/备份(参数透传给 sd-replace.sh)
  $0 format   [参数] # 直接执行格式化(参数透传给 sd-format.sh)
  $0 -h | --help
EOF
}

if [ $# -ge 1 ]; then
  case "$1" in
    migrate) shift; exec bash "$SCRIPT_DIR/sd-migrate.sh" "$@" ;;
    replace) shift; exec bash "$SCRIPT_DIR/sd-replace.sh" "$@" ;;
    format)  shift; exec bash "$SCRIPT_DIR/sd-format.sh"  "$@" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知命令: $1"; usage; exit 2 ;;
  esac
fi

echo "========== SD 卡工具箱 =========="
echo "1) SD 卡迁移 (eMMC -> SD)"
echo "2) SD 卡更换 (SD -> USB 备份)"
echo "3) SD 卡格式化 (分区 + ext4)"
echo "0) 退出"
read -r -p "请选择 [1/2/3/0]: " c
case "$c" in
  1) exec bash "$SCRIPT_DIR/sd-migrate.sh" ;;
  2) exec bash "$SCRIPT_DIR/sd-replace.sh" ;;
  3) exec bash "$SCRIPT_DIR/sd-format.sh" ;;
  0|"") echo "已退出"; exit 0 ;;
  *) echo "无效选择"; exit 1 ;;
esac
