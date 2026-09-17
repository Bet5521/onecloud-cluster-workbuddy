#!/bin/bash
# Clash/mihomo systemd 服务文件生成脚本

cat > /etc/systemd/system/mihomo.service << 'EOF'
[Unit]
Description=Mihomo Proxy Service
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/mihomo -d /mnt/sd/edge-01/clash
Restart=on-failure
RestartSec=5
LimitNOFILE=1048576
MemoryMax=64M

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable mihomo
systemctl start mihomo

echo "[✓] mihomo service 已安装并启动"
