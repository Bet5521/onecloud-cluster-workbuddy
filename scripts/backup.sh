#!/bin/bash
# ============================================================
# 备份脚本 (backup.sh)
# 备份各节点配置和数据
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKUP_DIR="${BACKUP_DIR:-/mnt/sd/backups}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# 节点清单统一从 inventory 读取 (支持 nodes.local.yaml / 环境变量自定义)
# shellcheck source=lib-nodes.sh
source "${SCRIPT_DIR}/lib-nodes.sh"
require_nodes

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

usage() {
    cat << EOF
用法: $0 <备份类型> [节点名]

备份类型:
  all           备份所有节点 (默认)
  config        仅备份配置文件
  data          仅备份应用数据
  node <name>   备份指定节点 (如 wk-edge-01)
  service <svc> 备份指定服务 (如 homeassistant)

示例:
  $0 all
  $0 config
  $0 node wk-edge-01
  $0 service homeassistant
EOF
}

BACKUP_TYPE="${1:-all}"
TARGET="${2:-}"

# 提前处理 --help/-h, 避免 mkdir 失败
case "$BACKUP_TYPE" in
    -h|--help) usage; exit 0 ;;
esac

mkdir -p "$BACKUP_DIR/$TIMESTAMP"

echo ""
echo "=========================================="
echo "  OneCloud Cluster - Backup"
echo "  时间: $TIMESTAMP"
echo "=========================================="
echo ""

backup_remote() {
    local NODE_IP=$1
    local REMOTE_PATH=$2
    local LOCAL_NAME=$3
    local DESC=$4

    if ssh -o ConnectTimeout=5 "root@${NODE_IP}" "test -e $REMOTE_PATH" 2>/dev/null; then
        log_info "备份 $DESC from $NODE_IP..."
        rsync -az "root@${NODE_IP}:${REMOTE_PATH}" \
            "${BACKUP_DIR}/${TIMESTAMP}/${LOCAL_NAME}/" 2>/dev/null
    else
        log_warn "跳过 (不存在): $NODE_IP:$REMOTE_PATH"
    fi
}

case "$BACKUP_TYPE" in
    all)
        log_info "全量备份开始..."

        # 遍历 inventory 中登记的全部节点
        for NODE in "${ALL_NODES[@]}"; do
            IFS='|' read -r NAME HOSTNAME IP WG_IP ROLE <<< "$NODE"
            [ -z "$IP" ] && { log_warn "跳过 $NAME (未配置 IP)"; continue; }
            backup_remote "$IP" "/mnt/sd/srv/${NAME}" "$HOSTNAME" "$NAME 全部"
            backup_remote "$IP" "/etc/wireguard" "${HOSTNAME}-wg" "WireGuard 配置"
        done

        # 本地项目
        if [ -d "$SCRIPT_DIR/.." ]; then
            log_info "备份项目代码..."
            rsync -az --exclude '.git' \
                "${SCRIPT_DIR}/.." "${BACKUP_DIR}/${TIMESTAMP}/project/"
        fi
        ;;

    config)
        log_info "仅备份配置文件..."

        for NODE in "${ALL_NODES[@]}"; do
            IFS='|' read -r NAME HOSTNAME IP WG_IP ROLE <<< "$NODE"
            [ -z "$IP" ] && continue
            backup_remote "$IP" "/mnt/sd/srv/*/docker-compose.yml" "config-${HOSTNAME}" "docker-compose"
            backup_remote "$IP" "/mnt/sd/srv/*/.env" "config-${HOSTNAME}-env" "env文件"
            backup_remote "$IP" "/etc/wireguard" "config-wg-${HOSTNAME}" "WireGuard"
            backup_remote "$IP" "/etc/systemd/system/mihomo.service" "config-svc-${HOSTNAME}" "systemd服务"
        done
        ;;

    node)
        NODE_NAME="${TARGET:-}"
        if [ -z "$NODE_NAME" ]; then
            NODE_NAME="${NODE_NAMES[0]}"
            log_info "未指定节点, 默认使用: $NODE_NAME"
        fi
        # 支持标准名 / 短名 / 主机名
        if ! NODE_NAME="$(node_resolve "$NODE_NAME")"; then
            log_error "未知节点: $TARGET (可用: $(node_names | tr '\n' ' '))"
            exit 1
        fi
        NODE_IP="$(node_ip "$NODE_NAME")"
        NODE_HOST="$(node_hostname "$NODE_NAME")"
        [ -z "$NODE_IP" ] && { log_error "节点 $NODE_NAME 未配置 IP"; exit 1; }

        backup_remote "$NODE_IP" "/mnt/sd/srv/$NODE_NAME" "${NODE_HOST}" "$NODE_NAME 全部"
        backup_remote "$NODE_IP" "/etc/wireguard" "${NODE_HOST}-wg" "WireGuard"
        ;;

    service)
        SVC="${TARGET:-homeassistant}"
        # 服务所在节点从 inventory/services.yaml 查询, 不硬编码
        if ! SVC_NODE="$(node_of_service "$SVC")"; then
            log_error "未知服务: $SVC (可用: $(service_names | tr '\n' ' '))"
            exit 1
        fi
        SVC_IP="$(node_ip "$SVC_NODE")"
        [ -z "$SVC_IP" ] && { log_error "服务 $SVC 所在节点 $SVC_NODE 未配置 IP"; exit 1; }
        log_info "服务 $SVC 位于 $SVC_NODE ($SVC_IP)"
        backup_remote "$SVC_IP" "/mnt/sd/srv/${SVC_NODE}/${SVC}" "${SVC}" "${SVC}"
        ;;

    *)
        usage
        exit 1
        ;;
esac

# 生成备份清单
cat > "${BACKUP_DIR}/${TIMESTAMP}/manifest.txt" << EOF
OneCloud Cluster 备份清单
生成时间: $(date)
备份类型: $BACKUP_TYPE
节点: ${TARGET:-全部}

目录结构:
$(find "${BACKUP_DIR}/${TIMESTAMP}" -type d | head -50)

大小:
$(du -sh "${BACKUP_DIR}/${TIMESTAMP}" 2>/dev/null)
EOF

echo ""
log_info "备份完成: ${BACKUP_DIR}/${TIMESTAMP}"
log_info "备份大小: $(du -sh "${BACKUP_DIR}/${TIMESTAMP}" 2>/dev/null | cut -f1)"
echo ""

# 清理旧备份 (保留最近 10 个)
log_info "清理旧备份..."
ls -1t "$BACKUP_DIR" 2>/dev/null | tail -n +11 | while read OLD; do
    rm -rf "${BACKUP_DIR}/${OLD}"
    log_warn "已删除旧备份: $OLD"
done
