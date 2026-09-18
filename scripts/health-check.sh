#!/bin/bash
# ============================================================
# 健康检查脚本 (health-check.sh)
# 检查各节点和服务状态
#
# 设计要点 (v1.6.0 起):
#   1. 服务清单来自 inventory/services.yaml + nodes.yaml, 不再逐条硬编码
#      —— 原实现把 18 个容器/端口写死在脚本里, 清单改了脚本不会跟着改。
#   2. 未安装 (installed=0) 的服务直接 SKIP, 不算失败。
#      典型症状: 不装 WireGuard 的机器上原脚本无条件 check WireGuard,
#      结果整份报告永远带一个 FAIL —— 用户无从判断是真故障还是没装。
#   3. 探测地址按组网模式取 (lan 用 LAN IP, wireguard 用 wg IP, mixed 先 LAN)
#      —— 由 lib-services.sh: probe_addr_for 统一决定。
# ============================================================

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 节点清单统一从 inventory 读取 (支持 nodes.local.yaml / 环境变量自定义)
# shellcheck source=lib-nodes.sh
source "${SCRIPT_DIR}/lib-nodes.sh"
# 安装态 / 组网模式 / 数据根 单一真相库
# shellcheck source=lib-services.sh
source "${SCRIPT_DIR}/lib-services.sh"
require_nodes

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

OK="${GREEN}[OK]${NC}"
FAIL="${RED}[FAIL]${NC}"
WARN="${YELLOW}[WARN]${NC}"
SKIP="${YELLOW}[SKIP]${NC}"

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

echo ""
echo "=========================================="
echo "  OneCloud Cluster - Health Check"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="
echo ""
echo "  组网模式: $(network_mode_label)"
echo "  WireGuard: $([ "$(wg_enabled)" = "1" ] && echo 启用 || echo 未启用)"
echo ""

check_ssh() {
    local ip=$1 name=$2
    if ssh -o ConnectTimeout=3 -o BatchMode=yes "root@${ip}" "echo ok" &>/dev/null; then
        echo -e "$OK  SSH: $name ($ip)"
        return 0
    else
        echo -e "$FAIL SSH: $name ($ip) - 无法连接"
        return 1
    fi
}

# check_container <IP> <显示名> <容器名>
# 修复: 容器名加引号并做白名单校验, 防止清单里的值把参数拆开
check_container() {
    local ip=$1 name=$2 container=$3
    case "$container" in
        ''|*[!A-Za-z0-9_.-]*) echo -e "$WARN $name - 容器名非法: '$container'"; return 0 ;;
    esac
    local status
    status=$(ssh "root@${ip}" "docker inspect --format='{{.State.Status}}' '${container}' 2>/dev/null" 2>/dev/null)
    if [ "$status" = "running" ]; then
        local mem
        mem=$(ssh "root@${ip}" "docker stats --no-stream --format '{{.MemUsage}}' '${container}' 2>/dev/null" 2>/dev/null)
        echo -e "$OK  $name ($container) - $mem"
    elif [ -z "$status" ]; then
        echo -e "$FAIL $name ($container) - 容器不存在"
    else
        echo -e "$FAIL $name ($container) - 状态: $status"
    fi
}

# check_port <IP> <端口> <显示名>
# 修复: 原 grep -q ':${port} ' 会漏掉 ss 输出的 "0.0.0.0:9000" 末列 (行尾无空格),
#       也会把 51820 误匹配到 518200; 改用 awk 精确比对末段端口。
check_port() {
    local ip=$1 port=$2 name=$3
    case "$port" in
        ''|0|*[!0-9]*) return 0 ;;   # 清单未声明端口 -> 静默跳过
    esac
    if ssh "root@${ip}" "ss -Htln 2>/dev/null | awk '{print \$4}' | grep -qE '[:.]${port}\$'" 2>/dev/null; then
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
    local load mem disk droot
    # 数据根以节点 /etc/onecloud/install.conf 为准; 取不到回退 /mnt/sd (兼容旧环境)
    droot="$(node_data_root "$ip")"
    load=$(ssh "root@${ip}" "cat /proc/loadavg | cut -d' ' -f1")
    mem=$(ssh "root@${ip}" "free -m | awk 'NR==2{printf \"%s/%sMB (%.0f%%)\", \$3,\$2,\$3/\$2*100}'")
    disk=$(ssh "root@${ip}" "df -h '${droot}' | awk 'NR==2{print \$4 \" 可用 / \" \$2 \" 总计 (\" \$5 \" 已用)\"}'")
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

# ----------------------------------------------------------------------------
# 按清单检查某节点的全部服务
#   已安装 -> 检查容器/端口
#   未安装 -> SKIP (不算失败)
# ----------------------------------------------------------------------------
check_node_services() {
    local node="$1" ip="$2" s inst ctype cname cport
    local checked=0 skipped=0

    for s in $(node_services "$node"); do
        inst="$(service_installed "$node" "$s")"
        if [ "$inst" != "1" ]; then
            # 区分「本来就不该装」与「需要人工安装」, 让用户一眼看懂原因
            local imode
            imode="$(services_install_mode "$s")"
            if [ "$imode" = "manual" ]; then
                echo -e "$WARN $s - 待手动安装 (services.yaml: install: manual)"
            elif [ "$s" = "wireguard" ]; then
                echo -e "$SKIP $s - 当前模式未启用 WireGuard ($(network_mode))"
            else
                echo -e "$SKIP $s - 未安装"
            fi
            skipped=$((skipped + 1))
            continue
        fi

        ctype="$(service_field "$s" container 2>/dev/null || true)"
        cport="$(services_port "$s")"

        # native 服务 (container: false) 只查端口, 不查容器
        if [ "$ctype" = "false" ]; then
            [ "$cport" != "0" ] && check_port "$ip" "$cport" "$s"
        else
            cname="$(service_field "$s" container_name 2>/dev/null || true)"
            [ -z "$cname" ] && cname="$s"
            check_container "$ip" "$s" "$cname"
            [ "$cport" != "0" ] && check_port "$ip" "$cport" "$s"
        fi
        checked=$((checked + 1))
    done

    echo "  小结: 已检查 $checked 项, 跳过 $skipped 项 (未安装)"
}

# ----------------------------------------------------------------------------
# 遍历全部节点 (不再按 edge/iot/storage 三个角色硬编码)
# ----------------------------------------------------------------------------
for NODE in $(node_names); do
    NODE_IP_ADDR="$(node_ip "$NODE")"
    NODE_HOSTNAME="$(node_hostname "$NODE")"
    [ -z "$NODE_IP_ADDR" ] && { log_warn "$NODE 未配置 IP, 跳过"; continue; }

    if ! check_system "$NODE_IP_ADDR" "$NODE ($NODE_HOSTNAME)"; then
        continue
    fi
    check_node_services "$NODE" "$NODE_IP_ADDR"
done

# ---- WireGuard Mesh (按模式判定, 未启用则明确 SKIP) ----
echo -e "\n--- WireGuard Mesh ---"
if [ "$(wg_enabled)" != "1" ]; then
    echo -e "$SKIP 当前组网模式为 $(network_mode), 未启用 WireGuard, 跳过检查"
    echo "       (这是配置选择, 不是故障; 如需启用改 inventory/nodes.yaml 的 network.mode)"
else
    WG_HUB_NAME="$(wg_hub_node 2>/dev/null || echo "")"
    WG_HUB_IP="$(wg_hub_ip 2>/dev/null || echo "")"
    if [ -z "$WG_HUB_IP" ]; then
        echo -e "$WARN 清单中未找到提供 WireGuard 的节点, 跳过"
    elif ssh "root@${WG_HUB_IP}" "wg show wg0 2>/dev/null" &>/dev/null; then
        echo -e "$OK  WireGuard Hub 运行中 ($WG_HUB_NAME / $WG_HUB_IP)"
        peers=$(ssh "root@${WG_HUB_IP}" "wg show wg0 2>/dev/null | grep -c 'endpoint'" 2>/dev/null || echo 0)
        echo "  Peer 连接数: $peers"
    else
        echo -e "$FAIL WireGuard Hub 未运行 ($WG_HUB_NAME / $WG_HUB_IP)"
    fi
fi

# ---- Syncthing 同步状态 (仅在该服务已安装时检查) ----
echo -e "\n--- Syncthing 同步状态 ---"
SYNC_NODE="$(node_of_service syncthing 2>/dev/null || echo "")"
if [ -z "$SYNC_NODE" ]; then
    echo "  清单中未定义 syncthing, 跳过"
elif [ "$(service_installed "$SYNC_NODE" syncthing)" != "1" ]; then
    echo "  syncthing 未安装, 跳过"
else
    SYNC_IP="$(probe_addr_for "$SYNC_NODE")"
    st_devices=$(ssh "root@${SYNC_IP}" "curl -s http://127.0.0.1:8384/rest/db/devices 2>/dev/null | jq 'length'" 2>/dev/null || echo "N/A")
    echo "  已知设备数: $st_devices"
fi

echo ""
echo "=========================================="
echo "  检查完成"
echo "=========================================="
