#!/bin/bash
# ============================================================
# WireGuard 配置生成脚本 (wireguard-setup.sh)
# 生成所有节点的 wg0.conf 和密钥
# ============================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

WG_DIR="$(cd "$(dirname "$0")" && pwd)/../wireguard"
mkdir -p "$WG_DIR"

echo ""
echo "=========================================="
echo "  WireGuard Mesh 配置生成器"
echo "=========================================="
echo ""

read -p "请输入域名 (如 yourdomain.com): " DOMAIN
DOMAIN="${DOMAIN:-yourdomain.com}"

NODES=(
    "wk-edge-01|192.168.1.101|10.8.0.101|edge-01"
    "wk-iot-02|192.168.1.102|10.8.0.102|iot-02"
    "wk-storage-03|192.168.1.103|10.8.0.103|storage-03"
)

# 生成所有密钥
echo -e "\n${GREEN}生成密钥对...${NC}"
declare -A PRIVATE_KEYS
declare -A PUBLIC_KEYS

for NODE in "${NODES[@]}"; do
    IFS='|' read -r NAME LAN_IP WG_IP HOSTNAME <<< "$NODE"
    KEY_FILE="$WG_DIR/${NAME}.key"
    PUB_FILE="$WG_DIR/${NAME}.pub"

    if [ ! -f "$KEY_FILE" ]; then
        wg genkey | tee "$KEY_FILE" | wg pubkey > "$PUB_FILE"
        log_info "$NAME 密钥已生成"
    else
        log_info "$NAME 密钥已存在, 跳过"
    fi

    PRIVATE_KEYS[$NAME]=$(cat "$KEY_FILE")
    PUBLIC_KEYS[$NAME]=$(cat "$PUB_FILE")
done

# 为每个节点生成 wg0.conf
echo -e "\n${GREEN}生成 wg0.conf...${NC}"

for NODE in "${NODES[@]}"; do
    IFS='|' read -r NAME LAN_IP WG_IP HOSTNAME <<< "$NODE"
    CONF_FILE="$WG_DIR/${NAME}-wg0.conf"

    log_info "生成 $CONF_FILE"

    cat > "$CONF_FILE" << WGEOF
# WireGuard 配置 - ${NAME}
# 自动生成于 $(date)

[Interface]
Address = ${WG_IP}/32
PrivateKey = ${PRIVATE_KEYS[$NAME]}
ListenPort = 51820
DNS = 10.8.0.101, 1.1.1.1

# NAT 穿透 (仅 edge 节点)
# PostUp = iptables -A FORWARD -i wg0 -j ACCEPT; iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
# PostDown = iptables -D FORWARD -i wg0 -j ACCEPT; iptables -t nat -D POSTROUTING -o eth0 -j MASQUERADE

WGEOF

    # 添加其他节点为 Peer
    for PEER in "${NODES[@]}"; do
        IFS='|' read -r PEER_NAME PEER_LAN_IP PEER_WG_IP PEER_HOSTNAME <<< "$PEER"

        if [ "$PEER_NAME" = "$NAME" ]; then
            continue
        fi

        # 优先使用域名端点 (如果是 edge 节点则用域名, 其他用 LAN IP)
        if [ "$PEER_NAME" = "wk-edge-01" ]; then
            ENDPOINT="${DOMAIN}:51820"
        else
            ENDPOINT="${PEER_LAN_IP}:51820"
        fi

        cat >> "$CONF_FILE" << WGEOF
# Peer: ${PEER_NAME}
[Peer]
PublicKey = ${PUBLIC_KEYS[$PEER_NAME]}
AllowedIPs = ${PEER_WG_IP}/32, ${PEER_LAN_IP}/32
Endpoint = ${ENDPOINT}
PersistentKeepalive = 25

WGEOF
    done
done

# 显示汇总
echo -e "\n${GREEN}密钥汇总 (用于交叉配置):${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
for NODE in "${NODES[@]}"; do
    IFS='|' read -r NAME _ _ _ _ <<< "$NODE"
    echo -e "  ${NAME}:"
    echo -e "    PrivateKey: ${YELLOW}${PRIVATE_KEYS[$NAME]:0:20}...${NC}"
    echo -e "    PublicKey:  ${YELLOW}${PUBLIC_KEYS[$NAME]}${NC}"
done
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

echo ""
log_info "WireGuard 配置已生成到: $WG_DIR"
log_info ""
log_warn "下一步:"
log_warn "  1. 将 ${NAME}-wg0.conf 复制到对应节点 /etc/wireguard/wg0.conf"
log_warn "  2. chmod 600 /etc/wireguard/wg0.conf"
log_warn "  3. systemctl enable wg-quick@wg0 && systemctl start wg-quick@wg0"
log_warn "  4. 在 edge 节点配置防火墙 (可选):"
log_warn "     ufw allow 51820/udp"
log_warn "     iptables -A INPUT -p udp --dport 51820 -j ACCEPT"
