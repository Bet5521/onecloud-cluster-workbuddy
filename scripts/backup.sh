#!/bin/bash
# ============================================================
# 备份脚本 (backup.sh)
# 备份各节点配置和数据
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKUP_DIR="/mnt/sd/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

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

        # Edge Gateway
        backup_remote 192.168.1.101 "/mnt/sd/srv/wk-edge-01" "edge-01" "NODE-01 全部"
        backup_remote 192.168.1.101 "/etc/wireguard" "edge-01-wg" "WireGuard 配置"

        # IoT Core
        backup_remote 192.168.1.102 "/mnt/sd/srv/wk-iot-02" "iot-02" "NODE-02 全部"

        # Storage
        backup_remote 192.168.1.103 "/mnt/sd/srv/wk-storage-03" "storage-03" "NODE-03 全部"

        # 本地项目
        if [ -d "$SCRIPT_DIR/.." ]; then
            log_info "备份项目代码..."
            rsync -az --exclude '.git' \
                "${SCRIPT_DIR}/.." "${BACKUP_DIR}/${TIMESTAMP}/project/"
        fi
        ;;

    config)
        log_info "仅备份配置文件..."

        for NODE_IP in 192.168.1.101 192.168.1.102 192.168.1.103; do
            backup_remote $NODE_IP "/mnt/sd/srv/*/docker-compose.yml" "config-$NODE_IP" "docker-compose"
            backup_remote $NODE_IP "/mnt/sd/srv/*/.env" "config-$NODE_IP-env" "env文件"
            backup_remote $NODE_IP "/etc/wireguard" "config-wg-$NODE_IP" "WireGuard"
            backup_remote $NODE_IP "/etc/systemd/system/mihomo.service" "config-svc-$NODE_IP" "systemd服务"
        done
        ;;

    node)
        NODE_NAME="${TARGET:-wk-edge-01}"
        NODE_IP=$(grep "$NODE_NAME" "$SCRIPT_DIR/../inventory/nodes.yaml" 2>/dev/null | grep -oP 'ip: \K[0-9.]+' | head -1)
        # Fallback: 硬编码节点映射
        if [ -z "$NODE_IP" ]; then
            case "$NODE_NAME" in
                wk-edge-01)   NODE_IP="192.168.1.101" ;;
                wk-iot-02)    NODE_IP="192.168.1.102" ;;
                wk-storage-03) NODE_IP="192.168.1.103" ;;
            esac
        fi
        [ -z "$NODE_IP" ] && { log_error "未知节点: $NODE_NAME"; exit 1; }

        backup_remote "$NODE_IP" "/mnt/sd/srv/$NODE_NAME" "${NODE_NAME}" "$NODE_NAME 全部"
        backup_remote "$NODE_IP" "/etc/wireguard" "${NODE_NAME}-wg" "WireGuard"
        ;;

    service)
        SVC="${TARGET:-homeassistant}"
        # Home Assistant 在 NODE-02
        if [ "$SVC" = "homeassistant" ]; then
            backup_remote 192.168.1.102 "/mnt/sd/srv/wk-iot-02/homeassistant" "homeassistant" "Home Assistant"
        elif [ "$SVC" = "piwigo" ]; then
            backup_remote 192.168.1.102 "/mnt/sd/srv/wk-iot-02/piwigo" "piwigo" "Piwigo"
        elif [ "$SVC" = "aria2" ]; then
            backup_remote 192.168.1.103 "/mnt/sd/srv/wk-storage-03/aria2" "aria2" "aria2"
        elif [ "$SVC" = "syncthing" ]; then
            backup_remote 192.168.1.103 "/mnt/sd/srv/wk-storage-03/syncthing" "syncthing" "Syncthing"
        elif [ "$SVC" = "gitea" ]; then
            backup_remote 192.168.1.103 "/mnt/sd/srv/wk-storage-03/gitea" "gitea" "Gitea"
        else
            log_error "未知服务: $SVC"
            exit 1
        fi
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
