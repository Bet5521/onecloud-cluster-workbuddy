#!/bin/bash
# verysync systemd 服务

VSYNC_DIR="$(cd "$(dirname "$0")" && pwd)"

cat > /etc/systemd/system/verysync.service << EOF
[Unit]
Description=VerySync File Sync Service
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/verysync -c ${VSYNC_DIR}/config.yaml
Restart=on-failure
RestartSec=5
WorkingDirectory=${VSYNC_DIR}
LimitNOFILE=4096
MemoryMax=64M

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable verysync
systemctl start verysync

echo "[✓] verysync service 已安装并启动"
echo "[*] WebUI: http://${NODE_IP}:19900"
