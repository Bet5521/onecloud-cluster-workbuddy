#!/bin/bash
# xiaomusic systemd 服务

cat > /etc/systemd/system/xiaomusic.service << 'EOF'
[Unit]
Description=Xiaomusic Music Service
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/xiaomusic -c /mnt/sd/iot-02/xiaomusic/config.json
Restart=on-failure
RestartSec=5
WorkingDirectory=/mnt/sd/iot-02/xiaomusic
LimitNOFILE=4096
MemoryMax=96M

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable xiaomusic
systemctl start xiaomusic

echo "[✓] xiaomusic service 已安装并启动"
