#!/bin/bash
# ============================================================
# OneCloud Cluster - SD 卡迁移脚本 (sd-migrate.sh)
#
# 场景: 初始化时未插 SD 卡, 组件被安装到 eMMC 回退目录
#       (/opt/onecloud, 见 lib-install-path.sh)。之后插入 SD 卡,
#       本脚本把 eMMC 上的全部组件 / 配置 / 依赖文件完整迁移到 SD 卡。
#
# 前置(脚本自动处理):
#   * 自动探测 SD 卡; 若文件系统非 ext4, 调用 sd-format.sh 自动格式化
#   * SD 卡未挂载则尝试挂载
#   * 建议迁移前停止相关容器/服务, 避免写入导致数据不一致
#
# 设计原则:
#   * 挂载点 / 设备一律运行时查询, 不写死路径
#   * 迁移失败不破坏来源(默认保留, --clean 才删除)
#   * 支持 --dry-run 预演
# ============================================================
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
if [ -f "${SCRIPT_DIR}/lib-install-path.sh" ]; then
  # shellcheck source=lib-install-path.sh
  source "${SCRIPT_DIR}/lib-install-path.sh"
fi
if [ -f "${SCRIPT_DIR}/sd-format.sh" ]; then
  # shellcheck source=sd-format.sh
  source "${SCRIPT_DIR}/sd-format.sh"
fi

# ---------------- 参数 ----------------
DRY_RUN=0
CLEAN_SRC=0
FORCE=0
ASSUME_YES=0
SRC_ROOT="${INSTALL_FALLBACK_ROOT:-/opt/onecloud}"
DOCKER_SRC="/var/lib/docker"

usage() {
  cat <<'EOF'
SD 卡迁移脚本 - 将无 SD 卡期间安装在 eMMC(/opt/onecloud) 的组件迁移到已挂载 SD 卡

前置条件:
  * 已插入 SD 卡(脚本会自动探测; 若文件系统非 ext4 会自动格式化为 ext4)
  * 建议先停止相关容器/服务, 避免迁移过程中数据写入导致不一致

用法:
  ./scripts/sd-migrate.sh [选项]

选项:
  --dry-run        只打印将要执行的操作, 不真正迁移
  --clean          迁移并校验通过后, 删除 eMMC 上的来源目录 (默认保留)
  --force          跳过"建议停服"提示
  --yes / -y       非交互确认 (与 --dry-run 配合可用于自动化)
  --source <目录>  指定 eMMC 来源根目录 (默认 /opt/onecloud)
  -h, --help       显示本帮助
EOF
}

main() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --dry-run) DRY_RUN=1; shift ;;
      --clean)   CLEAN_SRC=1; shift ;;
      --force)   FORCE=1; shift ;;
      --yes|-y) ASSUME_YES=1; shift ;;
      --source)  SRC_ROOT="$2"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) log_error "未知参数: $1"; usage; exit 2 ;;
    esac
  done

  # 1. SD 就绪: 探测 -> 必要时格式化(ext4) -> 挂载 -> 评估
  if ! ensure_sd_ready; then
    log_error "SD 卡未就绪, 无法迁移。请先插入并(让脚本)挂载 SD 卡。"
    exit 1
  fi
  local SD_MP="$SD_MOUNT_POINT"

  if [ "$SD_MP" = "$SRC_ROOT" ]; then
    log_error "SD 挂载点($SD_MP)与来源($SRC_ROOT)相同, 无需迁移"
    exit 1
  fi

  log_info "SD 卡就绪: ${SD_DEV} -> ${SD_MP}"
  log_info "来源(eMMC 回退目录): ${SRC_ROOT}"

  # 2. 交互确认
  if [ "$FORCE" != 1 ] && [ "$ASSUME_YES" != 1 ]; then
    echo ""
    echo "即将迁移:"
    echo "  应用数据: ${SRC_ROOT}  ->  ${SD_MP}"
    echo "  Docker 数据(若仍在 eMMC): ${DOCKER_SRC}  ->  ${SD_MP}/docker"
    echo ""
    read -r -p "确认继续? [y/N] " _ans
    case "$_ans" in y|Y|yes) ;; *) log_info "已取消"; exit 0 ;; esac
  fi

  # 3. 迁移应用数据
  migrate_data_root "$SRC_ROOT" "$SD_MP" || true

  # 4. 迁移 Docker 数据
  migrate_docker "$SD_MP" || true

  # 5. 校验
  if ! verify; then
    log_error "迁移已完成但校验未通过, 请检查 SD 卡状态"
    exit 1
  fi

  # 6. 清理来源(可选)
  if [ "$CLEAN_SRC" = 1 ] && [ "$DRY_RUN" != 1 ]; then
    log_info "按要求清理来源: ${SRC_ROOT}"
    rm -rf "$SRC_ROOT"
  else
    log_info "来源 ${SRC_ROOT} 已保留 (加 --clean 可删除)"
  fi

  log_info "迁移流程结束。"
  if [ "$DRY_RUN" = 1 ]; then
    log_info "(dry-run) 未做任何实际改动。"
  fi
}

migrate_data_root() {
  local src="$1" dst="$2"
  if [ ! -d "$src" ] || [ -z "$(ls -A "$src" 2>/dev/null)" ]; then
    log_info "来源 ${src} 为空或不存在, 跳过应用数据迁移"
    return 0
  fi
  log_info "迁移应用数据: ${src}/ -> ${dst}/"
  if [ "$DRY_RUN" = 1 ]; then
    log_info "[dry-run] rsync -aHAX ${src}/ ${dst}/"
    return 0
  fi
  mkdir -p "$dst"
  if rsync -aHAX --info=progress2 "${src}/" "${dst}/"; then
    log_info "应用数据已迁移到 ${dst}"
    return 0
  fi
  log_error "rsync 失败, 来源 ${src} 保持不变, 请手动处理"
  return 1
}

migrate_docker() {
  local sd_mp="$1"
  if [ ! -d "$DOCKER_SRC" ] || [ -z "$(ls -A "$DOCKER_SRC" 2>/dev/null)" ]; then
    log_info "Docker 数据不在 eMMC 默认位置($DOCKER_SRC), 跳过 Docker 迁移"
    return 0
  fi
  local ddst="${sd_mp}/docker"
  log_info "迁移 Docker 数据: ${DOCKER_SRC}/ -> ${ddst}/"
  if [ "$DRY_RUN" = 1 ]; then
    log_info "[dry-run] systemctl stop docker; rsync -aHAX ${DOCKER_SRC}/ ${ddst}/; 写 /etc/docker/daemon.json; systemctl start docker"
    return 0
  fi
  systemctl stop docker 2>/dev/null || service docker stop 2>/dev/null || true
  mkdir -p "$ddst"
  if ! rsync -aHAX "${DOCKER_SRC}/" "${ddst}/"; then
    log_error "Docker 数据迁移失败, 已跳过 daemon.json 改写"
    systemctl start docker 2>/dev/null || service docker start 2>/dev/null || true
    return 1
  fi
  mkdir -p /etc/docker
  cat > /etc/docker/daemon.json <<EOF
{
  "data-root": "${ddst}",
  "log-driver": "json-file",
  "log-opts": { "max-size": "5m", "max-file": "2" },
  "registry-mirrors": [ "https://dockerproxy.com", "https://mirror.baidubce.com" ]
}
EOF
  systemctl start docker 2>/dev/null || service docker start 2>/dev/null || true
  log_info "Docker 已指向 ${ddst}, 请确认容器状态正常"
  return 0
}

verify() {
  log_info "校验: 重新评估安装路径..."
  resolve_data_root
  if [ "$INSTALL_VIA_SD" = 1 ]; then
    log_info "OK: 安装路径已指向 SD 卡 ${DATA_ROOT}"
    return 0
  fi
  log_error "校验未通过: 仍回退到 ${DATA_ROOT} (${SD_REJECT_REASON})"
  return 1
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
