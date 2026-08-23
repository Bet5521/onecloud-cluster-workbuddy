#!/bin/bash
# OneCloud Cluster Panel - systemd 服务
# 复制到 /etc/systemd/system/ 后启用

PANEL_DIR="$(cd "$(dirname "$0")" && pwd)"
NODE_NAME="${NODE_NAME:-wk-edge-01}"
PANEL_SERVICE="${PANEL_DIR}/config.json"

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
Environment=PANEL_PORT=9000
Environment=PANEL_HOST=0.0.0.0
LimitNOFILE=4096
MemoryMax=128M

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable onecloud-panel
systemctl start onecloud-panel

echo "[✓] Panel service 已启动"
echo "    访问: http://$(hostname -I | awk '{print $1}'):9000"
