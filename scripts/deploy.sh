#!/bin/bash
# ============================================================
# 配置分发脚本 (deploy.sh)
# 将项目配置 rsync 到各节点
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# 默认节点列表
# 格式: 节点名|IP|目录后缀(node-<suffix>)
NODES=(
    "wk-edge-01|192.168.1.101|wk-edge-01"
    "wk-iot-02|192.168.1.102|wk-iot-02"
    "wk-storage-03|192.168.1.103|wk-storage-03"
)

usage() {
    cat << EOF
用法: $0 [选项]

选项:
  -n, --node NAME    指定节点名称 (如 wk-edge-01)
  -a, --all          分发到所有节点 (默认)
  -t, --test         仅测试连接, 不分发
  -d, --dry-run      显示将要传输的内容, 不实际传输
  -h, --help         显示帮助

示例:
  $0                      # 分发所有节点
  $0 -n wk-edge-01        # 仅分发到边缘网关
  $0 -t                   # 测试所有节点 SSH 连接
  $0 -d                   # 预览将传输的文件
EOF
}

DRY_RUN=""
TEST_ONLY=false
TARGET_NODES=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|--node)  TARGET_NODES+=("$2"); shift 2 ;;
        -a|--all)   TARGET_NODES=(); shift ;;
        -t|--test)  TEST_ONLY=true; shift ;;
        -d|--dry-run) DRY_RUN="--dry-run"; shift ;;
        -h|--help)  usage; exit 0 ;;
        *) log_error "未知选项: $1"; usage; exit 1 ;;
    esac
done

echo ""
echo "=========================================="
echo "  OneCloud Cluster - Config Deploy"
echo "=========================================="
echo ""

deploy_node() {
    local NODE_NAME=$1
    local NODE_IP=$2
    local NODE_ROLE=$3
    local NODE_SRC="${PROJECT_DIR}/node-${NODE_ROLE}"
    local REMOTE_BASE="/mnt/sd/srv/${NODE_NAME}"

    echo "--- $NODE_NAME ($NODE_IP) ---"

    # 测试 SSH 连接
    if ! ssh -o ConnectTimeout=5 "root@${NODE_IP}" "echo ok" &>/dev/null; then
        log_error "无法连接到 $NODE_NAME ($NODE_IP)"
        return 1
    fi
    log_info "SSH 连接成功: $NODE_NAME"

    if [ "$TEST_ONLY" = true ]; then
        return 0
    fi

    # 确保远程目录存在
    ssh "root@${NODE_IP}" "mkdir -p ${REMOTE_BASE}/{cloudflared,adguard/{work,conf},wireguard/config,clash,memos,homeassistant,piwigo/{config,gallery},xiaomusic,migpt,typecho,syncthing/{config,data},verysync,aria2/{config,downloads},cupsd/{config,printers,spool},cups-web,panel,gitea}"

    # 分发配置文件
    if [ -d "$NODE_SRC" ]; then
        log_info "分发 ${NODE_SRC} → ${NODE_IP}:${REMOTE_BASE}"
        rsync -avz $DRY_RUN \
            --exclude '*.bak' --exclude '*.old' \
            --exclude '.git' --exclude '__pycache__' \
            "${NODE_SRC}/" "root@${NODE_IP}:${REMOTE_BASE}/"
    else
        log_warn "本地未找到 $NODE_SRC, 跳过"
    fi

    # 分发脚本和文档到公共位置
    if [ "$DRY_RUN" != "--dry-run" ]; then
        ssh "root@${NODE_IP}" "mkdir -p /mnt/sd/scripts /mnt/sd/docs /mnt/sd/inventory"
        rsync -avz $DRY_RUN "${SCRIPT_DIR}/" "root@${NODE_IP}:/mnt/sd/scripts/"
        rsync -avz $DRY_RUN "${PROJECT_DIR}/docs/" "root@${NODE_IP}:/mnt/sd/docs/"
        rsync -avz $DRY_RUN "${PROJECT_DIR}/inventory/" "root@${NODE_IP}:/mnt/sd/inventory/"
    fi

    log_info "$NODE_NAME 完成 ✓"
    echo ""
}

if [ ${#TARGET_NODES[@]} -eq 0 ]; then
    TARGET_NODES=("${NODES[@]}")
fi

for NODE in "${TARGET_NODES[@]}"; do
    IFS='|' read -r NAME IP ROLE <<< "$NODE"
    deploy_node "$NAME" "$IP" "$ROLE" || true
done

echo "=========================================="
log_info "分发完成"
echo "=========================================="
