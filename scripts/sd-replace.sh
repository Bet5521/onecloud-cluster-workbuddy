#!/bin/bash
# ============================================================
# OneCloud Cluster - SD 卡更换 / 备份脚本 (sd-replace.sh)
#
# 场景: 需要更换 SD 卡前, 先把当前 SD 卡上的全部组件内容打包压缩,
#       转存到已插入的 USB 存储设备, 以便换卡后恢复。
#
# 前置(脚本会自动校验, 不通过则中止):
#   * 已插入 USB 存储设备并挂载
#   * USB 设备剩余空间 >= SD 卡数据量 + 预留余量
#   * SD 卡已就绪(自动探测; 非 ext4 会自动格式化并挂载)
#
# 设计原则:
#   * 设备 / 挂载点一律运行时查询, 不写死路径
#   * USB 设备绝不与 SD 卡 / 根盘混淆
#   * 支持 --dry-run 预演
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
if [ -f "${SCRIPT_DIR}/sd-format.sh" ]; then
  # shellcheck source=sd-format.sh
  source "${SCRIPT_DIR}/sd-format.sh"
fi

DRY_RUN=0
ASSUME_YES=0
USB_MP_OVERRIDE=""
SPACE_MARGIN_MB=512   # 预留余量, 避免打包中途空间不足

usage() {
  cat <<'EOF'
SD 卡更换 / 备份脚本 - 将 SD 卡全部组件打包压缩并转存到 USB 存储设备

前置条件(脚本会自动校验, 不通过则中止):
  * 已插入 USB 存储设备并挂载
  * USB 设备剩余空间 >= SD 卡数据量 + 预留余量(${SPACE_MARGIN_MB}MB)
  * SD 卡已就绪(自动探测; 非 ext4 会自动格式化为 ext4)

用法:
  ./scripts/sd-replace.sh [选项]
选项:
  --usb <挂载点>   指定 USB 挂载点(默认自动探测第一个满足空间的 USB 设备)
  --dry-run        只打印将要执行的操作, 不真正打包
  --yes / -y       非交互确认
  -h, --help       显示本帮助
EOF
}

# SD 卡内容大小(MB)
sd_content_size_mb() {
  local mp="$1"
  [ -d "$mp" ] || { echo 0; return 1; }
  du -sm "$mp" 2>/dev/null | awk '{print int($1)}'
}

# 探测可用 USB 设备: 选中挂载点写入 USB_MP, 失败返回 1
detect_usb() {
  local need_mb="$1" _name _rm _type _tran _part _mp _free
  USB_MP=""
  if [ -n "${USB_MP_OVERRIDE:-}" ]; then USB_MP="$USB_MP_OVERRIDE"; return 0; fi
  while read -r _name _rm _type _tran; do
    [ "$_type" = "disk" ] || continue
    [ "$_rm" = "1" ] || [ "$_tran" = "usb" ] || continue
    [ "$_name" = "${SD_DEV:-}" ] && continue      # 排除 SD 卡自身
    # 候选分区: 测试钩子直接给定(配合 OC_TEST_NO_SYSBLOCK), 否则按 /dev/<disk>* 探测
    local _cand
    if [ -n "${ONECLOUD_USB_TEST_PART:-}" ] && [ "${OC_TEST_NO_SYSBLOCK:-0}" = "1" ]; then
      _cand="${ONECLOUD_USB_TEST_PART}"
    else
      _cand="$(ls /dev/${_name}* 2>/dev/null)"
    fi
    for _part in $_cand; do
      if [ -z "${ONECLOUD_USB_TEST_PART:-}" ] && [ ! -e "$_part" ]; then continue; fi
      _mp="$(findmnt -n -o TARGET --source "$_part" 2>/dev/null | head -1)"
      [ -n "$_mp" ] && is_mountpoint "$_mp" || continue
      [ "$_mp" = "/" ] && continue                 # 排除根文件系统
      # 可用空间: 测试钩子直接给定, 否则按 df 实测 (绕过沙箱 /usr/bin 阴影)
      if [ -n "${ONECLOUD_USB_TEST_FREE_MB:-}" ]; then
        _free="${ONECLOUD_USB_TEST_FREE_MB}"
      else
        _free="$(df -Pm "$_mp" 2>/dev/null | awk 'NR==2{print int($4)}')"
      fi
      [ -n "$_free" ] || continue
      if [ "$_free" -ge "$need_mb" ]; then
        USB_MP="$_mp"; return 0
      fi
    done
  done < <(lsblk -dno NAME,RM,TYPE,TRAN 2>/dev/null || true)
  return 1
}

main() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --usb)     USB_MP_OVERRIDE="$2"; shift 2 ;;
      --dry-run) DRY_RUN=1; shift ;;
      --yes|-y)  ASSUME_YES=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) log_error "未知参数: $1"; usage; exit 2 ;;
    esac
  done

  # 1. SD 卡就绪(探测 / 必要时格式化 ext4 / 挂载)
  if ! ensure_sd_ready; then
    log_error "SD 卡未就绪, 无法备份。请先插入并(让脚本)挂载 SD 卡。"
    exit 1
  fi
  local SD_MP="$SD_MOUNT_POINT"
  local need
  need="$(sd_content_size_mb "$SD_MP")"
  need=$(( ${need:-0} + SPACE_MARGIN_MB ))
  log_info "SD 卡内容大小约 ${need}MB (含预留 ${SPACE_MARGIN_MB}MB), 挂载点 ${SD_MP}"

  # 2. 校验 USB 设备
  if ! detect_usb "$need"; then
    log_error "未找到满足条件的 USB 存储设备:"
    log_error "  * 需已插入 USB 设备并挂载"
    log_error "  * 剩余空间需 >= ${need}MB"
    log_error "请插入容量足够的 USB 设备后重试。"
    exit 1
  fi
  log_info "将备份到 USB 设备: ${USB_MP}"

  # 3. 确认
  if [ "$ASSUME_YES" != 1 ]; then
    read -r -p "确认将 SD 卡内容打包备份到 ${USB_MP}? [y/N] " _ans
    case "$_ans" in y|Y|yes) ;; *) log_info "已取消"; exit 0 ;; esac
  fi

  # 4. 打包
  pack "$SD_MP" "$USB_MP" || exit 1
  log_info "SD 卡更换备份完成。可安全拔出 SD 卡并更换, 之后用 sd-migrate.sh 反向导入(如有需要)。"
}

pack() {
  local sd_mp="$1" usb_mp="$2"
  local host="$(hostname 2>/dev/null || echo unknown)"
  local stamp="$(date +%Y%m%d_%H%M%S)"
  local arch="${usb_mp}/onecloud-sd-backup-${host}-${stamp}.tar.gz"
  log_info "打包 SD 卡内容: ${sd_mp}/ -> ${arch}"
  if [ "$DRY_RUN" = 1 ]; then
    log_info "[dry-run] tar -czf ${arch} -C ${sd_mp} ."
    return 0
  fi
  if tar -czf "$arch" -C "$sd_mp" . 2>/dev/null; then
    :
  else
    # 退化: 不用内置 gzip, 手动管道
    tar -cf - -C "$sd_mp" . 2>/dev/null | gzip > "$arch" 2>/dev/null || true
  fi
  if [ ! -s "$arch" ]; then
    log_error "打包失败或产物为空: ${arch}"
    return 1
  fi
  local sum="$(sha256sum "$arch" 2>/dev/null | awk '{print $1}')"
  local sz="$(du -h "$arch" 2>/dev/null | awk '{print $1}')"
  log_info "已完成备份: ${arch}"
  log_info "  大小: ${sz}  SHA256: ${sum}"
  return 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
