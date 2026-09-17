#!/bin/bash
# aria2 初始化脚本

ARIA_DIR="$(dirname "$0")"
mkdir -p "$ARIA_DIR/config" "$ARIA_DIR/downloads"

# 创建空 session 文件
touch "$ARIA_DIR/config/aria2.session"

# 下载最新 tracker 列表
echo "[*] 更新 BT tracker 列表..."
curl -sL "https://raw.githubusercontent.com/XIU2/TrackersListCollection/master/all_aria2.txt" \
    -o "$ARIA_DIR/config/trackers-list.txt" 2>/dev/null || \
curl -sL "https://trackerslist.com/all_aria2.txt" \
    -o "$ARIA_DIR/config/trackers-list.txt" 2>/dev/null || true

# 更新 aria2.conf 中的 tracker
if [ -f "$ARIA_DIR/config/trackers-list.txt" ]; then
    TRACKERS=$(cat "$ARIA_DIR/config/trackers-list.txt" | grep -v '^#' | grep -v '^$' | paste -sd, -)
    if [ -n "$TRACKERS" ]; then
        sed -i "s|^bt-tracker=.*|bt-tracker=${TRACKERS}|" "$ARIA_DIR/aria2.conf"
        echo "[✓] Tracker 已更新: $(echo $TRACKERS | tr ',' '\n' | wc -l) 个"
    fi
fi

# 复制配置文件到 config 目录（如果不存在）
if [ ! -f "$ARIA_DIR/config/aria2.conf" ]; then
    cp "$ARIA_DIR/aria2.conf" "$ARIA_DIR/config/aria2.conf"
fi

chown -R 1000:1000 "$ARIA_DIR" 2>/dev/null || true

echo "[✓] aria2 目录已初始化"
echo "[*] 访问 AriaNg: http://${NODE_IP}:6880"
echo "[*] RPC Secret: ${ARIA2_RPC_SECRET} (在 .env 中修改)"
