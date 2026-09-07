#!/bin/bash
# ============================================================
# WireGuard 配置生成脚本 (wireguard-setup.sh)
# 生成各节点的 wg0.conf 和密钥, 并写入 node-<name>/wireguard/
# 以便 deploy.sh 能直接分发到各节点
#
# 用法:
#   ./scripts/wireguard-setup.sh                       # 生成/刷新全部节点配置
#   ./scripts/wireguard-setup.sh gen [-d DOMAIN]       # 同上
#   ./scripts/wireguard-setup.sh add peer <name> <lan_ip> <wg_ip>
#   ./scripts/wireguard-setup.sh list                  # 列出已登记节点
# ============================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# 密钥与节点登记表的存放位置 (不入库)
WG_DIR="${PROJECT_DIR}/wireguard"
mkdir -p "$WG_DIR"
PEERS_FILE="${WG_DIR}/peers.list"
DOMAIN_FILE="${WG_DIR}/domain"

# 内置节点 (集群固定成员)
DEFAULT_NODES=(
    "wk-edge-01|192.168.1.101|10.8.0.101|edge-01"
    "wk-iot-02|192.168.1.102|10.8.0.102|iot-02"
    "wk-storage-03|192.168.1.103|10.8.0.103|storage-03"
)

# 域名: 优先参数 > 状态文件 > 交互输入 > 默认值
DOMAIN=""
if [ -f "$DOMAIN_FILE" ]; then
    DOMAIN="$(head -1 "$DOMAIN_FILE" 2>/dev/null || true)"
fi
[ -z "$DOMAIN" ] && DOMAIN="yourdomain.com"

usage() {
    cat << EOF
用法: $0 <命令> [参数]

命令:
  gen                                    生成全部节点密钥与 wg0.conf (默认)
  add peer <名称> <LAN_IP> <WG_IP>       登记一个新节点并重新生成全部配置
  list                                   列出已登记的所有节点
  help                                   显示本帮助

选项 (用于 gen):
  -d, --domain DOMAIN    WireGuard 端点域名 (默认: ${DOMAIN})

示例:
  $0
  $0 gen --domain example.com
  $0 add peer wk-backup-04 192.168.1.104 10.8.0.104
EOF
}

# 载入已登记节点: 内置节点 + peers.list 中新增的节点
load_nodes() {
    NODES=("${DEFAULT_NODES[@]}")
    if [ -f "$PEERS_FILE" ]; then
        while IFS='|' read -r name lan_ip wg_ip hostname; do
            [ -z "${name:-}" ] && continue
            case "$name" in \#*) continue ;; esac
            NODES+=("${name}|${lan_ip}|${wg_ip}|${hostname:-${name#wk-}}")
        done < "$PEERS_FILE"
    fi
}

# 为单个节点生成密钥 (若不存在)
ensure_key() {
    local name=$1
    local key_file="${WG_DIR}/${name}.key"
    local pub_file="${WG_DIR}/${name}.pub"
    if [ ! -f "$key_file" ] || [ ! -f "$pub_file" ]; then
        if ! command -v wg >/dev/null 2>&1; then
            log_error "未找到 wg 命令, 请先安装: apt install -y wireguard-tools"
            return 1
        fi
        umask 077
        wg genkey | tee "$key_file" | wg pubkey > "$pub_file"
        log_info "$name 密钥已生成"
    fi
    chmod 600 "$key_file" 2>/dev/null || true
    PRIVATE_KEYS[$name]="$(cat "$key_file")"
    PUBLIC_KEYS[$name]="$(cat "$pub_file")"
}

# 生成某个节点的 wg0.conf, 写入 node-<name>/wireguard/wg0.conf
write_node_conf() {
    local name=$1 lan_ip=$2 wg_ip=$3
    local out_dir="${PROJECT_DIR}/node-${name}/wireguard"
    local conf="${out_dir}/wg0.conf"

    mkdir -p "$out_dir"

    # 端点: edge 节点用域名 (可被外网访问), 其余用 LAN IP
    local endpoint
    if [ "$name" = "wk-edge-01" ]; then
        endpoint="${DOMAIN}:51820"
    else
        endpoint="${lan_ip}:51820"
    fi

    {
        echo "# WireGuard 配置 - ${name}"
        echo "# 由 scripts/wireguard-setup.sh 自动生成于 $(date)"
        echo "# 本文件含私钥, 请勿提交到版本库"
        echo ""
        echo "[Interface]"
        echo "Address = ${wg_ip}/32"
        echo "PrivateKey = ${PRIVATE_KEYS[$name]}"
        echo "ListenPort = 51820"
        echo "DNS = 10.8.0.101, 1.1.1.1"
        echo ""
        if [ "$name" = "wk-edge-01" ]; then
            echo "# 作为 Hub 转发流量 (PostUp/PostDown 各只允许出现一次)"
            echo "PostUp = iptables -A FORWARD -i wg0 -j ACCEPT; iptables -A FORWARD -o wg0 -j ACCEPT; iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE; iptables -A INPUT -p udp --dport 51820 -j ACCEPT"
            echo "PostDown = iptables -D FORWARD -i wg0 -j ACCEPT; iptables -D FORWARD -o wg0 -j ACCEPT; iptables -t nat -D POSTROUTING -o eth0 -j MASQUERADE; iptables -D INPUT -p udp --dport 51820 -j ACCEPT"
            echo ""
        fi
    } > "$conf"

    local peer peer_name peer_lan peer_wg peer_host
    for peer in "${NODES[@]}"; do
        IFS='|' read -r peer_name peer_lan peer_wg peer_host <<< "$peer"
        [ "$peer_name" = "$name" ] && continue

        local peer_endpoint
        if [ "$peer_name" = "wk-edge-01" ]; then
            peer_endpoint="${DOMAIN}:51820"
        else
            peer_endpoint="${peer_lan}:51820"
        fi

        {
            echo "# Peer: ${peer_name}"
            echo "[Peer]"
            echo "PublicKey = ${PUBLIC_KEYS[$peer_name]}"
            echo "AllowedIPs = ${peer_wg}/32, ${peer_lan}/32"
            echo "Endpoint = ${peer_endpoint}"
            echo "PersistentKeepalive = 25"
            echo ""
        } >> "$conf"
    done

    chmod 600 "$conf"
    log_info "已生成 node-${name}/wireguard/wg0.conf (端点: ${endpoint})"
}

cmd_gen() {
    echo ""
    echo "=========================================="
    echo "  WireGuard Mesh 配置生成器"
    echo "=========================================="
    echo ""

    load_nodes

    declare -gA PRIVATE_KEYS PUBLIC_KEYS

    log_info "生成/复用密钥对..."
    local node name
    for node in "${NODES[@]}"; do
        IFS='|' read -r name _ _ _ <<< "$node"
        ensure_key "$name"
    done

    log_info "生成 wg0.conf..."
    local lan_ip wg_ip
    for node in "${NODES[@]}"; do
        IFS='|' read -r name lan_ip wg_ip _ <<< "$node"
        write_node_conf "$name" "$lan_ip" "$wg_ip"
    done

    echo "$DOMAIN" > "$DOMAIN_FILE"

    echo ""
    log_info "公钥汇总 (用于交叉核对):"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    for node in "${NODES[@]}"; do
        IFS='|' read -r name _ _ _ <<< "$node"
        echo "  ${name}: ${PUBLIC_KEYS[$name]}"
    done
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    echo ""
    log_info "配置已写入各 node-<名称>/wireguard/wg0.conf"
    log_warn "下一步:"
    log_warn "  1. 运行 ./scripts/deploy.sh 分发配置到各节点"
    log_warn "  2. 在节点上: cp wg0.conf /etc/wireguard/wg0.conf && chmod 600 /etc/wireguard/wg0.conf"
    log_warn "  3. systemctl enable --now wg-quick@wg0"
    log_warn "  4. edge 节点放行 UDP 51820"
}

cmd_add_peer() {
    local new_name=${1:-} new_lan=${2:-} new_wg=${3:-}

    if [ -z "$new_name" ] || [ -z "$new_lan" ] || [ -z "$new_wg" ]; then
        log_error "参数不足"
        echo "用法: $0 add peer <名称> <LAN_IP> <WG_IP>"
        echo "示例: $0 add peer wk-backup-04 192.168.1.104 10.8.0.104"
        exit 1
    fi

    load_nodes

    # 检查重名 / IP 冲突 (注意: 循环变量不可复用入参变量名)
    local node n_name n_lan n_wg
    for node in "${NODES[@]}"; do
        IFS='|' read -r n_name n_lan n_wg _ <<< "$node"
        if [ "$n_name" = "$new_name" ]; then
            log_error "节点已存在: $new_name (如需修改请先编辑 ${PEERS_FILE})"
            exit 1
        fi
        if [ "$n_lan" = "$new_lan" ]; then
            log_error "LAN IP 与已有节点 $n_name 冲突: $new_lan"
            exit 1
        fi
        if [ "$n_wg" = "$new_wg" ]; then
            log_error "WireGuard IP 与已有节点 $n_name 冲突: $new_wg"
            exit 1
        fi
    done

    echo "${new_name}|${new_lan}|${new_wg}|${new_name#wk-}" >> "$PEERS_FILE"
    log_info "已登记新节点: $new_name (${new_lan} / ${new_wg})"

    # 重新生成全部配置, 使新节点加入全互联 mesh
    cmd_gen

    log_info "新节点 ${new_name} 已加入 mesh, 请重新分发配置到所有节点"
}

cmd_list() {
    load_nodes
    echo ""
    echo "已登记节点:"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    printf "  %-16s %-16s %-14s %s\n" "名称" "LAN IP" "WG IP" "主机名"
    local node name lan_ip wg_ip host
    for node in "${NODES[@]}"; do
        IFS='|' read -r name lan_ip wg_ip host <<< "$node"
        printf "  %-16s %-16s %-14s %s\n" "$name" "$lan_ip" "$wg_ip" "$host"
    done
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
}

# ---- 入口 ----
COMMAND="${1:-gen}"
shift || true

case "$COMMAND" in
    gen|generate|"")
        while [[ $# -gt 0 ]]; do
            case "$1" in
                -d|--domain) DOMAIN="$2"; shift 2 ;;
                *) log_error "未知选项: $1"; usage; exit 1 ;;
            esac
        done
        cmd_gen
        ;;
    add)
        SUBCMD="${1:-}"
        shift || true
        case "$SUBCMD" in
            peer) cmd_add_peer "${1:-}" "${2:-}" "${3:-}" ;;
            *) log_error "未知子命令: add ${SUBCMD}"; usage; exit 1 ;;
        esac
        ;;
    list|ls)
        cmd_list
        ;;
    help|-h|--help)
        usage
        ;;
    *)
        log_error "未知命令: $COMMAND"
        usage
        exit 1
        ;;
esac
