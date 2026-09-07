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
# 注意: PostUp/PostDown 每个只能出现一次, 多条规则用分号连接
# (重复写 PostUp 时 wg-quick 只保留最后一个, 前面会被静默丢弃)
cat > wg0.conf << EOF
[Interface]
Address = ${WG_ADDR}/32
ListenPort = ${WG_PORT}
PrivateKey = $(cat server_private.key)

# DNS 路由到 AdGuard
# 转发 + NAT + 放行 WireGuard 入站
PostUp = iptables -A FORWARD -i wg0 -j ACCEPT; iptables -A FORWARD -o wg0 -j ACCEPT; iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE; iptables -A INPUT -p udp --dport ${WG_PORT} -j ACCEPT
PostDown = iptables -D FORWARD -i wg0 -j ACCEPT; iptables -D FORWARD -o wg0 -j ACCEPT; iptables -t nat -D POSTROUTING -o eth0 -j MASQUERADE; iptables -D INPUT -p udp --dport ${WG_PORT} -j ACCEPT

EOF

echo "[✓] WireGuard 密钥和基础配置已生成在 $WG_DIR"
echo ""
echo "请将各 peer 的 Public Key 填入其他节点的 wg0.conf"
