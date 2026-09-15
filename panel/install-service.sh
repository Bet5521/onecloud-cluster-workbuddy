#!/bin/bash
# OneCloud Cluster Panel - systemd 服务
# 复制到 /etc/systemd/system/ 后启用

PANEL_DIR="$(cd "$(dirname "$0")" && pwd)"
NODE_NAME="${NODE_NAME:-wk-edge-01}"
PANEL_SERVICE="${PANEL_DIR}/config.json"

# 监听参数可由环境变量注入 (init/init.sh 会导出), 未提供时保持原默认值
PANEL_HOST="${PANEL_HOST:-0.0.0.0}"
PANEL_PORT="${PANEL_PORT:-9000}"

cat > /etc/systemd/system/onecloud-panel.service << EOF
[Unit]
Description=OneCloud Cluster Control Panel
After=network.target

[Service]
Type=simple
WorkingDirectory=${PANEL_DIR}
ExecStart=/usr/bin/python3 ${PANEL_DIR}/app.py
Restart=on-failure
RestartSec=5
Environment=PANEL_CONFIG=${PANEL_SERVICE}
Environment=PANEL_PORT=${PANEL_PORT}
Environment=PANEL_HOST=${PANEL_HOST}
LimitNOFILE=4096
MemoryMax=128M

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable onecloud-panel
systemctl start onecloud-panel

echo "[✓] Panel service 已启动"
echo "    监听: http://${PANEL_HOST}:${PANEL_PORT}"
echo "    访问: http://$(hostname -I | awk '{print $1}'):${PANEL_PORT}"
