#!/bin/bash
# ============================================================
# 健康检查脚本 (health-check.sh)
# 检查各节点和服务状态
# ============================================================

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 节点清单统一从 inventory 读取 (支持 nodes.local.yaml / 环境变量自定义)
# shellcheck source=lib-nodes.sh
source "${SCRIPT_DIR}/lib-nodes.sh"
require_nodes

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

OK="${GREEN}[OK]${NC}"
FAIL="${RED}[FAIL]${NC}"
WARN="${YELLOW}[WARN]${NC}"

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

echo ""
echo "=========================================="
echo "  OneCloud Cluster - Health Check"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="
echo ""

check_ssh() {
    local ip=$1 name=$2
    if ssh -o ConnectTimeout=3 "root@${ip}" "echo ok" &>/dev/null; then
        echo -e "$OK  SSH: $name ($ip)"
        return 0
    else
        echo -e "$FAIL SSH: $name ($ip) - 无法连接"
        return 1
    fi
}

check_container() {
    local ip=$1 name=$2 container=$3
    local status
    status=$(ssh "root@${ip}" "docker inspect --format='{{.State.Status}}' $container 2>/dev/null" 2>/dev/null)
    if [ "$status" = "running" ]; then
        local mem
        mem=$(ssh "root@${ip}" "docker stats --no-stream --format '{{.MemUsage}}' $container 2>/dev/null" 2>/dev/null)
        echo -e "$OK  $name ($container) - $mem"
    else
        echo -e "$FAIL $name ($container) - 状态: $status"
    fi
}

check_port() {
    local ip=$1 port=$2 name=$3
    if ssh "root@${ip}" "ss -tlnp | grep -q ':${port} '" 2>/dev/null; then
        echo -e "$OK  Port $port ($name)"
    else
        echo -e "$FAIL Port $port ($name) - 未监听"
    fi
}

check_system() {
    local ip=$1 name=$2

    echo -e "\n--- $name ($ip) ---"

    # SSH
    check_ssh "$ip" "$name" || return 1

    # 系统负载
    local load mem disk
    load=$(ssh "root@${ip}" "cat /proc/loadavg | cut -d' ' -f1")
    mem=$(ssh "root@${ip}" "free -m | awk 'NR==2{printf \"%s/%sMB (%.0f%%)\", \$3,\$2,\$3/\$2*100}'")
    disk=$(ssh "root@${ip}" "df -h /mnt/sd | awk 'NR==2{print \$4 \" 可用 / \" \$2 \" 总计 (\" \$5 \" 已用)\"}'")
    local swap
    swap=$(ssh "root@${ip}" "free -m | awk 'NR==3{printf \"%s/%sMB\", \$3,\$2}'")

    echo "  负载: $load"
    echo "  内存: $mem"
    echo "  Swap: $swap"
    echo "  磁盘: $disk"

    # OOM 检查
    local oom
    oom=$(ssh "root@${ip}" "dmesg 2>/dev/null | grep -c 'Killed process' || echo 0")
    if [ "$oom" -gt 0 ]; then
        echo -e "  ${FAIL} OOM Kill 记录: $oom 次"
    fi
}

# ---- 按角色取各节点 (函数由 lib-nodes.sh 提供) ----
EDGE_NAME="$(node_name_by_role edge-gateway    || echo "")"
IOT_NAME="$(node_name_by_role iot-core         || echo "")"
STORE_NAME="$(node_name_by_role storage-sync   || echo "")"

EDGE_IP="$( [ -n "$EDGE_NAME" ]   && node_ip "$EDGE_NAME"   || echo "")"
IOT_IP="$(  [ -n "$IOT_NAME" ]    && node_ip "$IOT_NAME"    || echo "")"
STORE_IP="$( [ -n "$STORE_NAME" ] && node_ip "$STORE_NAME"  || echo "")"

EDGE_HOST="$(  [ -n "$EDGE_NAME" ]  && node_hostname "$EDGE_NAME"  || echo "")"
IOT_HOST="$(   [ -n "$IOT_NAME" ]   && node_hostname "$IOT_NAME"   || echo "")"
STORE_HOST="$( [ -n "$STORE_NAME" ] && node_hostname "$STORE_NAME" || echo "")"

# WireGuard Hub 取边缘网关节点, Syncthing 取存储节点
WG_HUB_IP="$EDGE_IP"
SYNC_IP="$STORE_IP"

# ---- 边缘网关 (Edge Gateway) ----
if [ -n "$EDGE_IP" ]; then
check_system "$EDGE_IP" "$EDGE_NAME ($EDGE_HOST)"
check_container "$EDGE_IP" "cloudflared" "cloudflared"
check_container "$EDGE_IP" "AdGuard Home" "adguard"
check_container "$EDGE_IP" "WireGuard" "wireguard"
check_container "$EDGE_IP" "Memos" "memos"
check_port "$EDGE_IP" 3000 "AdGuard Web"
check_port "$EDGE_IP" 5230 "Memos"
check_port "$EDGE_IP" "${NET_WG_PORT}" "WireGuard"
else
log_warn "未定义边缘网关节点 (role: edge-gateway), 跳过该节点检查"
fi

# ---- IoT 核心 ----
if [ -n "$IOT_IP" ]; then
check_system "$IOT_IP" "$IOT_NAME ($IOT_HOST)"
check_container "$IOT_IP" "Home Assistant" "homeassistant"
check_container "$IOT_IP" "Piwigo" "piwigo"
check_port "$IOT_IP" 8123 "Home Assistant"
check_port "$IOT_IP" 8080 "Piwigo"
check_port "$IOT_IP" 8081 "xiaomusic"
check_container "$IOT_IP" "Typecho" "typecho"
check_port "$IOT_IP" 8082 "migpt"
check_port "$IOT_IP" 8083 "Typecho"
else
log_warn "未定义 IoT 节点 (role: iot-core), 跳过该节点检查"
fi

# ---- 存储与同步 ----
if [ -n "$STORE_IP" ]; then
check_system "$STORE_IP" "$STORE_NAME ($STORE_HOST)"
check_container "$STORE_IP" "Syncthing" "syncthing"
check_container "$STORE_IP" "aria2" "aria2"
check_container "$STORE_IP" "CUPS" "cupsd"
check_container "$STORE_IP" "CUPS Web" "cups-web"
check_container "$STORE_IP" "AriaNg" "ariang"
check_container "$STORE_IP" "Gitea" "gitea"
check_port "$STORE_IP" 8384 "Syncthing Web"
check_port "$STORE_IP" 6800 "aria2 RPC"
check_port "$STORE_IP" 631 "CUPS"
check_port "$STORE_IP" 3000 "Gitea Web"
else
log_warn "未定义存储节点 (role: storage-sync), 跳过该节点检查"
fi

# ---- WireGuard Mesh ----
echo -e "\n--- WireGuard Mesh ---"
if [ -z "$WG_HUB_IP" ]; then
    echo -e "$WARN 未定义 WireGuard Hub 节点, 跳过"
elif ssh "root@${WG_HUB_IP}" "wg show wg0 2>/dev/null" &>/dev/null; then
    echo -e "$OK  WireGuard Hub 运行中 ($WG_HUB_IP)"
    peers=$(ssh "root@${WG_HUB_IP}" "wg show wg0 2>/dev/null | grep -c 'endpoint'" 2>/dev/null || echo 0)
    echo "  Peer 连接数: $peers"
else
    echo -e "$FAIL WireGuard Hub 未运行 ($WG_HUB_IP)"
fi

# ---- Syncthing 状态 ----
echo -e "\n--- Syncthing 同步状态 ---"
if [ -z "$SYNC_IP" ]; then
    echo "  未定义存储节点, 跳过"
else
    st_devices=$(ssh "root@${SYNC_IP}" "curl -s http://127.0.0.1:8384/rest/db/devices 2>/dev/null | jq 'length'" 2>/dev/null || echo "N/A")
    echo "  已知设备数: $st_devices"
fi

echo ""
echo "=========================================="
echo "  检查完成"
echo "=========================================="
