#!/bin/bash
# ============================================================
# 批量更新脚本 (update-all.sh)
# 更新所有节点的 Docker 镜像和系统包
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 节点清单统一从 inventory 读取 (支持 nodes.local.yaml / 环境变量自定义)
# shellcheck source=lib-nodes.sh
source "${SCRIPT_DIR}/lib-nodes.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

usage() {
    cat << EOF
用法: $0 [选项]

选项:
  -d, --docker       仅更新 Docker 镜像 (默认)
  -s, --system       仅更新系统包
  -a, --all          更新 Docker + 系统 (排除 Docker Engine)
  -n, --node NAME    指定节点 (如 wk-edge-01)
  -h, --help         显示帮助

注意: 不会升级 Docker Engine, 已 apt-mark hold
EOF
}

MODE="docker"
TARGET=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -d|--docker) MODE="docker"; shift ;;
        -s|--system) MODE="system"; shift ;;
        -a|--all)    MODE="all"; shift ;;
        -n|--node)   TARGET="$2"; shift 2 ;;
        -h|--help)   usage; exit 0 ;;
        *) log_error "未知选项: $1"; usage; exit 1 ;;
    esac
done

require_nodes

# -n/--node 支持标准名 / 短名 / 主机名
if [ -n "$TARGET" ]; then
    if ! TARGET="$(node_resolve "$TARGET")"; then
        log_error "未知节点: $TARGET (可用: $(node_names | tr '\n' ' '))"
        exit 1
    fi
fi

echo ""
echo "=========================================="
echo "  OneCloud Cluster - Update"
echo "  模式: $MODE"
echo "=========================================="
echo ""

for NODE in "${ALL_NODES[@]}"; do
    IFS='|' read -r NAME HOSTNAME IP WG_IP ROLE <<< "$NODE"

    if [ -n "$TARGET" ] && [ "$NAME" != "$TARGET" ]; then
        continue
    fi

    echo "=========================================="
    echo "  $NAME ($IP, $HOSTNAME)"
    echo "=========================================="

    if ! ssh -o ConnectTimeout=3 "root@${IP}" "echo ok" &>/dev/null; then
        log_error "无法连接到 $NAME, 跳过"
        continue
    fi

    # Docker 更新
    if [ "$MODE" = "docker" ] || [ "$MODE" = "all" ]; then
        log_info "更新 Docker 镜像..."
        ssh "root@${IP}" "bash -s" << 'ENDSSH'
set -e
for dir in /mnt/sd/srv/*/; do
    if [ -f "${dir}docker-compose.yml" ]; then
        echo "  更新 $(basename $dir)..."
        cd "$dir"
        docker-compose pull 2>/dev/null || docker compose pull 2>/dev/null || echo "    pull 失败, 跳过"
    fi
done
ENDSSH
    fi

    # 系统更新
    if [ "$MODE" = "system" ] || [ "$MODE" = "all" ]; then
        log_info "更新系统包 (排除 docker)..."
        ssh "root@${IP}" "bash -s" << 'ENDSSH'
set -e
apt update -qq
# 获取可升级包, 排除 docker 相关
# 注意: apt list 首行是 "Listing... Done" 表头, 必须过滤掉,
#       否则会被当作包名传给 apt upgrade 导致报错
UPGRADABLE=$(apt list --upgradable 2>/dev/null | grep -vi docker | grep 'upgradable from' || true)
# 只取包名: 以 / 分隔的第一段, 无 / 的行(表头)会被丢弃
PKGS=$(echo "$UPGRADABLE" | awk -F'/' 'NF>1{print $1}' | tr '\n' ' ')
if [ -n "${PKGS// /}" ]; then
    echo "  可升级包:"
    echo "$UPGRADABLE"
    apt upgrade -y --no-install-recommends $PKGS
else
    echo "  无需更新"
fi
ENDSSH
    fi

    log_info "$NAME 完成 ✓"
    echo ""
done

echo "=========================================="
log_info "更新完成"
log_info "建议: docker-compose down && docker-compose up -d 重启服务"
echo "=========================================="
