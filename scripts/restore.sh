#!/bin/bash
# ============================================================
# 恢复脚本 (restore.sh)
# 从备份恢复配置和数据
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 节点清单统一从 inventory 读取 (支持 nodes.local.yaml / 环境变量自定义)
# shellcheck source=lib-nodes.sh
source "${SCRIPT_DIR}/lib-nodes.sh"
require_nodes

BACKUP_DIR="${BACKUP_DIR:-/mnt/sd/backups}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

usage() {
    cat << EOF
用法: $0 <备份ID> <恢复目标>

备份ID:   时间戳 (如 20260814_030000) 或 'latest'
恢复目标: all | config | node <name> | service <name>

示例:
  $0 latest all                 # 恢复最新备份到所有节点
  $0 20260814_030000 node wk-iot-02
  $0 latest service homeassistant
  $0 latest config

可用备份:
$(ls -1t "$BACKUP_DIR" 2>/dev/null | head -10)
EOF
}

BACKUP_ID="${1:-}"
ARG2="${2:-all}"
ARG3="${3:-}"

# 节点名归一化: edge-01 -> wk-edge-01
normalize_node() {
    case "$1" in
        wk-*) echo "$1" ;;
        *)    echo "wk-$1" ;;
    esac
}

# 兼容三种写法:
#   $0 <备份ID> all|config
#   $0 <备份ID> node    <节点名>
#   $0 <备份ID> service <服务名>
#   $0 <备份ID> <节点名或服务名>        (自动识别, 运维手册中的简写形式)
RESTORE_TARGET=""
RESTORE_NAME=""
KNOWN_SERVICES="homeassistant piwigo typecho aria2 syncthing gitea"

case "$ARG2" in
    all|config)
        RESTORE_TARGET="$ARG2"
        ;;
    node|service)
        RESTORE_TARGET="$ARG2"
        RESTORE_NAME="${ARG3:-}"
        ;;
    "")
        RESTORE_TARGET="all"
        ;;
    *)
        NODE_CANDIDATE=$(normalize_node "$ARG2")
        case "$NODE_CANDIDATE" in
            wk-edge-01|wk-iot-02|wk-storage-03)
                RESTORE_TARGET="node"
                RESTORE_NAME="$NODE_CANDIDATE"
                ;;
            *)
                if echo " $KNOWN_SERVICES " | grep -q " $ARG2 "; then
                    RESTORE_TARGET="service"
                    RESTORE_NAME="$ARG2"
                else
                    log_error "无法识别的恢复目标: $ARG2"
                    echo ""
                    echo "  可选: all | config | node <节点名> | service <服务名>"
                    echo "  已知节点: wk-edge-01 wk-iot-02 wk-storage-03 (可简写 edge-01)"
                    echo "  已知服务: $KNOWN_SERVICES"
                    exit 1
                fi
                ;;
        esac
        ;;
esac

if [ -z "$BACKUP_ID" ]; then
    usage
    exit 1
fi

# 解析 'latest'
if [ "$BACKUP_ID" = "latest" ]; then
    BACKUP_ID=$(ls -1t "$BACKUP_DIR" 2>/dev/null | head -1)
fi

BACKUP_PATH="${BACKUP_DIR}/${BACKUP_ID}"

if [ ! -d "$BACKUP_PATH" ]; then
    log_error "备份不存在: $BACKUP_PATH"
    echo ""
    echo "可用备份:"
    ls -1t "$BACKUP_DIR" 2>/dev/null | head -10
    exit 1
fi

echo ""
echo "=========================================="
echo "  OneCloud Cluster - Restore"
echo "  备份: $BACKUP_ID"
echo "  目标: $RESTORE_TARGET"
echo "=========================================="
echo ""
echo "将覆盖现有配置和数据! 确定继续?"
read -p "[y/N] " CONFIRM
[[ "$CONFIRM" =~ ^[Yy]$ ]] || { log_warn "已取消"; exit 0; }

# 恢复函数
restore_to_node() {
    local NODE_IP=$1
    local LOCAL_SRC=$2
    local REMOTE_DST=$3
    local DESC=$4

    if [ ! -d "$LOCAL_SRC" ]; then
        log_warn "跳过 (无备份): $DESC"
        return
    fi

    log_info "恢复 $DESC → $NODE_IP"
    ssh "root@${NODE_IP}" "mkdir -p ${REMOTE_DST}"
    rsync -az "$LOCAL_SRC/" "root@${NODE_IP}:${REMOTE_DST}/"
}

case "$RESTORE_TARGET" in
    all)
        # 遍历 inventory 中登记的全部节点; 备份子目录名 = 主机名
        for NODE in "${ALL_NODES[@]}"; do
            IFS='|' read -r NAME HOSTNAME IP WG_IP ROLE <<< "$NODE"
            [ -z "$IP" ] && { log_warn "跳过 $NAME (未配置 IP)"; continue; }
            restore_to_node "$IP" "${BACKUP_PATH}/${HOSTNAME}"    "/mnt/sd/srv/${NAME}" "${NAME}"
            restore_to_node "$IP" "${BACKUP_PATH}/${HOSTNAME}-wg" "/etc/wireguard"      "${NAME} WireGuard"
        done
        ;;

    config)
        for NODE in "${ALL_NODES[@]}"; do
            IFS='|' read -r NAME HOSTNAME IP WG_IP ROLE <<< "$NODE"
            [ -z "$IP" ] && continue
            restore_to_node "$IP" "${BACKUP_PATH}/${HOSTNAME}/config" "/mnt/sd/srv/${NAME}" "${NAME} 配置"
        done
        ;;

    node)
        NODE_NAME=$(normalize_node "${RESTORE_NAME:-}")
        if [ -z "$NODE_NAME" ]; then
            NODE_NAME="${NODE_NAMES[0]}"
            log_info "未指定节点, 默认使用: $NODE_NAME"
        fi
        # 支持标准名 / 短名 / 主机名
        if ! NODE_NAME="$(node_resolve "$NODE_NAME")"; then
            log_error "未知节点: ${RESTORE_NAME} (可用: $(node_names | tr '\n' ' '))"
            exit 1
        fi
        NODE_IP="$(node_ip "$NODE_NAME")"
        NODE_HOST="$(node_hostname "$NODE_NAME")"
        [ -z "$NODE_IP" ] && { log_error "节点 $NODE_NAME 未配置 IP"; exit 1; }

        # 备份目录名用主机名 (与 backup.sh 保持一致); 兼容旧备份的短名
        BACKUP_SUBDIR=$(ls -d "${BACKUP_PATH}/${NODE_HOST}"* "${BACKUP_PATH}/${NODE_NAME#wk-}"* "${BACKUP_PATH}/${NODE_NAME}"* 2>/dev/null | head -1 || true)
        [ -z "$BACKUP_SUBDIR" ] && { log_error "备份中未找到 $NODE_NAME (查找: ${NODE_HOST} / ${NODE_NAME})"; exit 1; }

        restore_to_node "$NODE_IP" "$BACKUP_SUBDIR" "/mnt/sd/srv/${NODE_NAME}" "$NODE_NAME"
        ;;

    service)
        SVC="${RESTORE_NAME:-homeassistant}"
        # 服务所在节点从 inventory/services.yaml 查询, 不硬编码
        if ! SVC_NODE="$(node_of_service "$SVC")"; then
            log_error "未知服务: $SVC (可用: $(service_names | tr '\n' ' '))"
            exit 1
        fi
        SVC_IP="$(node_ip "$SVC_NODE")"
        [ -z "$SVC_IP" ] && { log_error "服务 $SVC 所在节点 $SVC_NODE 未配置 IP"; exit 1; }
        log_info "服务 $SVC 位于 $SVC_NODE ($SVC_IP)"
        restore_to_node "$SVC_IP" "${BACKUP_PATH}/${SVC}" "/mnt/sd/srv/${SVC_NODE}/${SVC}" "${SVC}"
        ;;

    *)
        usage
        exit 1
        ;;
esac

echo ""
log_info "恢复完成"
log_warn "请重启相关服务或节点以应用更改"
log_info "  cd /mnt/sd/srv/<node> && docker-compose up -d"
