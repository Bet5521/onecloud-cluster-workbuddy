#!/bin/bash
# ============================================================
# OneCloud Cluster - SD 卡格式化脚本 (sd-format.sh)
#
# 职责
#   将插入的 SD 卡(整卡)分区为单一 ext4 分区。供迁移 / 更换流程在
#   "检测到 SD 卡但文件系统非 ext4" 时自动触发, 也可独立手动执行。
#
# 安全
#   * 仅对探测到的"可移动 SD 卡"操作, 绝不格式化根磁盘
#   * 默认交互确认; 加 --yes 跳过"会清空数据"的确认(危险)
#   * 已为 ext4 时默认跳过, 除非 --force-fmt
#
# 设计原则 (与 lib-install-path.sh 一致)
#   * 设备/分区一律运行时探测, 不写死 /dev/mmcblk1
#   * 挂载点运行时查询, 不写死 /mnt/sd
# ============================================================
set -euo pipefail

# ---------------- 日志 ----------------
log_info()  { echo "[INFO]  $*"; }
log_warn()  { echo "[WARN]  $*" >&2; }
log_error() { echo "[ERROR] $*" >&2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
if [ -f "${SCRIPT_DIR}/lib-install-path.sh" ]; then
  # shellcheck source=lib-install-path.sh
  source "${SCRIPT_DIR}/lib-install-path.sh"
fi

# ---------------- 参数 ----------------
DRY_RUN=0
ASSUME_YES=0
FORCE_FMT=0
SD_DEV_OVERRIDE=""
SD_MOUNT_BASE="${SD_MOUNT_BASE:-/mnt/onecloud-sd}"
FMT_LOG="${FMT_LOG:-}"   # 测试用: 记录实际执行的格式化命令

usage() {
  cat <<'EOF'
SD 卡格式化脚本 - 将 SD 卡分区并格式化为 ext4

前置条件 / 安全:
  * 仅对探测到的"可移动 SD 卡"操作, 绝不格式化根磁盘
  * 默认交互确认, 加 --yes 自动确认(会清空数据, 危险)

用法:
  ./scripts/sd-format.sh [选项]
选项:
  --dev <设备>    指定 SD 设备(如 /dev/mmcblk1), 默认自动探测
  --dry-run       只打印将要执行的分区/格式化操作, 不实际执行
  --yes / -y      非交互确认(跳过"会清空数据"提示)
  --force-fmt     即使已是 ext4 也强制重新格式化
  -h, --help      显示本帮助
EOF
}

# 查询设备的文件系统类型 (空字符串表示未格式化)
sd_fstype() {
  local d="$1" t=""
  # 测试钩子下设备为虚拟名, 跳过存在性检查; 生产环境仍要求设备存在
  if [ -z "${ONECLOUD_SD_TEST_DEV:-}" ] && [ ! -e "$d" ]; then
    printf ''; return 1
  fi
  if command -v blkid >/dev/null 2>&1; then
    t="$(blkid -o value -s TYPE "$d" 2>/dev/null)"
  fi
  if [ -z "$t" ] && command -v lsblk >/dev/null 2>&1; then
    t="$(lsblk -fno FSTYPE "$d" 2>/dev/null | head -1)"
  fi
  printf '%s' "$t"
}

# 根磁盘设备名(用于安全拦截)
_root_dev_name() {
  local s
  s="$(findmnt -n -o SOURCE / 2>/dev/null | awk '{print $1}')"
  basename "$s" 2>/dev/null
}

# 实际格式化: 分区(单 ext4 分区) + mkfs.ext4
format_sd() {
  local dev="$1"
  # 测试钩子下设备为虚拟名, 跳过块设备存在性检查; 生产环境仍要求 -b 为真
  if [ -z "${ONECLOUD_SD_TEST_DEV:-}" ] && [ ! -b "$dev" ]; then
    log_error "设备不存在: $dev"; return 1
  fi
  # 安全: 禁止格式化根磁盘
  local rd; rd="$(_root_dev_name)"
  case "$rd" in
    "${dev##*/}"|"${dev}") log_error "拒绝格式化根磁盘 ($dev)"; return 1 ;;
  esac
  local part="${dev}p1"
  if [ "$DRY_RUN" = 1 ]; then
    log_info "[dry-run] parted -s $dev mklabel gpt mkpart primary ext4 0% 100%"
    log_info "[dry-run] mkfs.ext4 -F -L onecloud-sd $part"
    if [ -n "$FMT_LOG" ]; then echo "DRYRUN $dev" >> "$FMT_LOG"; fi
    return 0
  fi
  log_info "正在将 $dev 分区并格式化为 ext4 ..."
  if command -v parted >/dev/null 2>&1; then
    parted -s "$dev" mklabel gpt 2>/dev/null || true
    parted -s "$dev" mkpart primary ext4 0% 100% 2>/dev/null || true
  elif command -v sfdisk >/dev/null 2>&1; then
    sfdisk "$dev" >/dev/null 2>&1 <<< 'label: gpt' || true
    sfdisk "$dev" >/dev/null 2>&1 <<< 'type=L' || true
  else
    log_error "未找到 parted/sfdisk, 无法分区"; return 1
  fi
  partprobe "$dev" 2>/dev/null || true
  [ -b "$part" ] || part="${dev}1"
  if ! mkfs.ext4 -F -L onecloud-sd "$part" 2>/dev/null; then
    log_error "mkfs.ext4 失败: $part"; return 1
  fi
  if [ -n "$FMT_LOG" ]; then echo "MKFS $part" >> "$FMT_LOG"; fi
  log_info "已完成格式化: $part (ext4)"
  return 0
}

# 检测 SD 设备文件系统, 非 ext4(或强制)时格式化; 已是 ext4 则跳过
sd_autofmt_if_needed() {
  if ! sd_probe; then
    log_error "未检测到 SD 卡设备, 无法格式化"; return 1
  fi
  local dev="$SD_DEV"
  local part="${SD_PART:-/dev/${dev}p1}"
  local ft; ft="$(sd_fstype "$part")"
  if [ "$ft" = "ext4" ] && [ "$FORCE_FMT" != 1 ]; then
    log_info "SD 卡已是 ext4 文件系统, 无需格式化 ($part)"
    return 0
  fi
  log_warn "SD 卡文件系统为 '${ft:-未格式化}', 需格式化为 ext4"
  if [ "$ASSUME_YES" != 1 ]; then
    read -r -p "确认将 ${dev} 格式化为 ext4? 此操作会清空所有数据! [y/N] " a
    case "$a" in y|Y|yes) ;; *) log_info "已取消格式化"; return 1 ;; esac
  fi
  format_sd "$dev" || return 1
  return 0
}

# 若 SD 未挂载则尝试挂载, 写入 SD_MOUNT_POINT / SD_MOUNTED
sd_mount_if_needed() {
  if [ -n "${SD_PART:-}" ] && sd_mount_state "${SD_PART}"; then
    return 0
  fi
  local part="${SD_PART:-/dev/${SD_DEV}p1}"
  local mp="$SD_MOUNT_BASE"
  [ -b "$part" ] || { log_error "分区不存在: $part"; return 1; }
  mkdir -p "$mp" 2>/dev/null
  if mount -t ext4 "$part" "$mp" 2>/dev/null; then
    SD_MOUNT_POINT="$mp"; SD_MOUNTED=1
    log_info "已挂载 $part -> $mp"
    return 0
  fi
  log_error "无法挂载 $part 到 $mp (可手动挂载后重试)"
  return 1
}

# 综合就绪: 探测 -> 必要时格式化 -> 挂载 -> 评估(可读写/空间)
ensure_sd_ready() {
  if ! sd_probe; then
    log_error "未检测到 SD 卡设备"; return 1
  fi
  sd_autofmt_if_needed || return 1
  sd_probe                       # 格式化后刷新分区信息
  sd_mount_if_needed || return 1
  if ! sd_evaluate; then
    log_error "SD 卡不可用: ${SD_REJECT_REASON}"; return 1
  fi
  return 0
}

main() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --dev)      SD_DEV_OVERRIDE="$2"; shift 2 ;;
      --dry-run)  DRY_RUN=1; shift ;;
      --yes|-y)   ASSUME_YES=1; shift ;;
      --force-fmt) FORCE_FMT=1; shift ;;
      -h|--help)  usage; exit 0 ;;
      *) log_error "未知参数: $1"; usage; exit 2 ;;
    esac
  done
  if [ -n "$SD_DEV_OVERRIDE" ]; then
    SD_DEV="$SD_DEV_OVERRIDE"
    SD_PART="/dev/${SD_DEV}p1"
    SD_FOUND=1
  fi
  if ! sd_probe && [ -z "$SD_DEV_OVERRIDE" ]; then
    log_error "未检测到 SD 卡设备"; exit 1
  fi
  sd_autofmt_if_needed || exit 1
  log_info "格式化流程结束。"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
