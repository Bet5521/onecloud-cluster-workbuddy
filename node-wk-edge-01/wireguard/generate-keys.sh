#!/bin/bash
# WireGuard 密钥生成脚本
# 运行一次生成 server 密钥和 peer 配置
#
# 本节点的 WireGuard 隧道 IP 取自 inventory/nodes.yaml (wk-edge-01.wg_ip),
# 可通过环境变量 ONECLOUD_WK_EDGE_01_WG_IP 覆盖, 无需改动本脚本。

WG_DIR="$(dirname "$0")/config"
mkdir -p "$WG_DIR"

# 尝试从清单库读取 edge 节点 WG IP (失败则回退默认)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB="${SCRIPT_DIR}/../../scripts/lib-nodes.sh"
WG_ADDR="10.8.0.101"
WG_PORT="51820"
if [ -f "$LIB" ]; then
    # shellcheck disable=SC1090
    source "$LIB" 2>/dev/null || true
    resolved="$(node_wg_ip wk-edge-01 2>/dev/null || true)"
    [ -n "$resolved" ] && WG_ADDR="$resolved"
    [ -n "${NET_WG_PORT:-}" ] && WG_PORT="$NET_WG_PORT"
fi

cd "$WG_DIR"

# 出网网卡: 本脚本在节点本地运行, 所以"现在"探测到的就是本机真实出口。
# 写死 eth0 是历史 bug —— 玩客云刷 Armbian 后网卡常是 end0, 那样 MASQUERADE
# 会静默失效 (表现: wg 握手正常, 客户端却上不了网)。
WG_EGRESS_IF="$(ip -4 route show default scope global 2>/dev/null | awk '/dev/{print $5; exit}')"
[ -n "$WG_EGRESS_IF" ] || WG_EGRESS_IF="eth0"
echo "[*] 出网网卡: ${WG_EGRESS_IF}"

echo "[*] 生成 WireGuard Server 密钥..."
if [ ! -f server_private.key ]; then
    wg genkey | tee server_private.key | wg pubkey > server_public.key
    echo "Server Private Key: $(cat server_private.key)"
    echo "Server Public Key:  $(cat server_public.key)"
else
    echo "[!] Server 密钥已存在, 跳过"
fi

echo "[*] 生成 Peer 密钥..."
for peer in 01 02 03; do
    if [ ! -f "peer${peer}_private.key" ]; then
        wg genkey | tee "peer${peer}_private.key" | wg pubkey > "peer${peer}_public.key"
        echo "Peer${peer} Private: $(cat peer${peer}_private.key)"
        echo "Peer${peer} Public:  $(cat peer${peer}_public.key)"
    fi
done

echo "[*] 生成 wg0.conf (WireGuard 地址: ${WG_ADDR}/32)..."

# 是否在 wg0.conf 里写 iptables 规则 —— 默认 **不写**。
# 防火墙策略统一由 setup_firewall.sh 管理, 免得 wg-quick 与它互相覆盖,
# 也免得反复 up/down 之后没人说得清规则是谁加的。
WG_FIREWALL="${ONECLOUD_WG_FIREWALL:-0}"
case "$WG_FIREWALL" in
    1|true|yes|on) WG_FIREWALL=1 ;;
    *)             WG_FIREWALL=0 ;;
esac

cat > wg0.conf << EOF
[Interface]
Address = ${WG_ADDR}/32
ListenPort = ${WG_PORT}
PrivateKey = $(cat server_private.key)

# DNS 路由到 AdGuard
EOF

if [ "$WG_FIREWALL" = "1" ]; then
    # 注意: PostUp/PostDown 每个只能出现一次, 多条规则用分号连接
    # (重复写 PostUp 时 wg-quick 只保留最后一个, 前面会被静默丢弃)
    cat >> wg0.conf << EOF

# 转发 + NAT + 放行 WireGuard 入站
#   - 每条都用 -C 先探测再添加: 反复 up 不会把规则堆成一摞
#     (堆起来后 PostDown 只删一条, 残留规则会让"到底谁在拦"变得很难查)
#   - 出网网卡取运行时探测值 ${WG_EGRESS_IF}, 不再写死 eth0
PostUp = iptables -C FORWARD -i wg0 -j ACCEPT 2>/dev/null || iptables -A FORWARD -i wg0 -j ACCEPT; iptables -C FORWARD -o wg0 -j ACCEPT 2>/dev/null || iptables -A FORWARD -o wg0 -j ACCEPT; iptables -t nat -C POSTROUTING -o ${WG_EGRESS_IF} -j MASQUERADE 2>/dev/null || iptables -t nat -A POSTROUTING -o ${WG_EGRESS_IF} -j MASQUERADE; iptables -C INPUT -p udp --dport ${WG_PORT} -j ACCEPT 2>/dev/null || iptables -A INPUT -p udp --dport ${WG_PORT} -j ACCEPT
PostDown = iptables -D FORWARD -i wg0 -j ACCEPT 2>/dev/null; iptables -D FORWARD -o wg0 -j ACCEPT 2>/dev/null; iptables -t nat -D POSTROUTING -o ${WG_EGRESS_IF} -j MASQUERADE 2>/dev/null; iptables -D INPUT -p udp --dport ${WG_PORT} -j ACCEPT 2>/dev/null || true

EOF
else
    cat >> wg0.conf << 'EOF'

# 本配置**不含任何防火墙规则** (默认行为)。
# 防火墙策略统一由 setup_firewall.sh 管理 —— 免得两边互相覆盖。
#
# 本机作为 Hub 转发流量, 需要下面三条 (手工或由集中式防火墙落地):
#   iptables -C FORWARD -i wg0 -j ACCEPT || iptables -A FORWARD -i wg0 -j ACCEPT
#   iptables -C FORWARD -o wg0 -j ACCEPT || iptables -A FORWARD -o wg0 -j ACCEPT
#   WG_IF=$(ip -4 route show default scope global | awk '{print $5; exit}')
#   iptables -t nat -C POSTROUTING -o "$WG_IF" -j MASQUERADE || \
#       iptables -t nat -A POSTROUTING -o "$WG_IF" -j MASQUERADE
#
# 入站端口用 DSL 表达即可:  in accept udp <WG端口> - -
# 若要恢复成 wg-quick 自带规则: ONECLOUD_WG_FIREWALL=1 重跑本脚本

EOF
fi

echo "[✓] WireGuard 密钥和基础配置已生成在 $WG_DIR"
echo ""
echo "请将各 peer 的 Public Key 填入其他节点的 wg0.conf"
