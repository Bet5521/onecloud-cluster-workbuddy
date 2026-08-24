#!/bin/bash
# ============================================================
# 健康检查脚本 (health-check.sh)
# 检查各节点和服务状态
# ============================================================

set -u

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

# ---- NODE-01 ----
check_system 192.168.1.101 "wk-edge-01"
check_container 192.168.1.101 "cloudflared" "cloudflared"
check_container 192.168.1.101 "AdGuard Home" "adguard"
check_container 192.168.1.101 "WireGuard" "wireguard"
check_container 192.168.1.101 "Memos" "memos"
check_port 192.168.1.101 3000 "AdGuard Web"
check_port 192.168.1.101 5230 "Memos"
check_port 192.168.1.101 51820 "WireGuard"

# ---- NODE-02 ----
check_system 192.168.1.102 "wk-iot-02"
check_container 192.168.1.102 "Home Assistant" "homeassistant"
check_container 192.168.1.102 "Piwigo" "piwigo"
check_port 192.168.1.102 8123 "Home Assistant"
check_port 192.168.1.102 8080 "Piwigo"
check_port 192.168.1.102 8081 "xiaomusic"
check_port 192.168.1.102 8082 "migpt"

# ---- NODE-03 ----
check_system 192.168.1.103 "wk-storage-03"
check_container 192.168.1.103 "Syncthing" "syncthing"
check_container 192.168.1.103 "aria2" "aria2"
check_container 192.168.1.103 "CUPS" "cupsd"
check_container 192.168.1.103 "CUPS Web" "cups-web"
check_container 192.168.1.103 "AriaNg" "ariang"
check_port 192.168.1.103 8384 "Syncthing Web"
check_port 192.168.1.103 6800 "aria2 RPC"
check_port 192.168.1.103 631 "CUPS"

# ---- WireGuard Mesh ----
echo -e "\n--- WireGuard Mesh ---"
if ssh "root@192.168.1.101" "wg show wg0 2>/dev/null" &>/dev/null; then
    echo -e "$OK  WireGuard Hub 运行中"
    peers=$(ssh "root@192.168.1.101" "wg show wg0 2>/dev/null | grep -c 'endpoint'" 2>/dev/null || echo 0)
    echo "  Peer 连接数: $peers"
else
    echo -e "$FAIL WireGuard Hub 未运行"
fi

# ---- Syncthing 状态 ----
echo -e "\n--- Syncthing 同步状态 ---"
st_devices=$(ssh "root@192.168.1.103" "curl -s http://127.0.0.1:8384/rest/db/devices 2>/dev/null | jq 'length'" 2>/dev/null || echo "N/A")
echo "  已知设备数: $st_devices"

echo ""
echo "=========================================="
echo "  检查完成"
echo "=========================================="
