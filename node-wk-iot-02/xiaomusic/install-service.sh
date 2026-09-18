#!/bin/bash
# xiaomusic systemd 服务
#
# 数据目录不写死: 安装时解析本节点实际数据根后再渲染单元。
set -e

# ---- 解析本节点数据根 (与 scripts/lib-services.sh 的约定一致) ----
resolve_data_root_here() {
    if [ -r /etc/onecloud/install.conf ]; then
        local dr
        dr="$(sed -n 's/^DATA_ROOT=//p' /etc/onecloud/install.conf | head -n1)"
        [ -n "$dr" ] && { printf '%s' "$dr"; return 0; }
    fi
    local m
    for m in /mnt/sd /mnt/data /media/sd; do
        if mountpoint -q "$m" 2>/dev/null && [ -w "$m" ]; then
            printf '%s' "$m"; return 0
        fi
    done
    printf '%s' "${ONECLOUD_REMOTE_DATA_ROOT:-/opt/onecloud}"
}

DATA_ROOT="${__DATA_ROOT__:-}"
case "$DATA_ROOT" in
    ""|"__DATA_ROOT__") DATA_ROOT="$(resolve_data_root_here)" ;;
esac
NODE_NAME="${__NODE_NAME__:-}"
case "$NODE_NAME" in
    ""|"__NODE_NAME__") NODE_NAME="$(hostname | sed 's/\..*//')" ;;
esac
XM_DIR="${DATA_ROOT}/srv/${NODE_NAME}/xiaomusic"

mkdir -p "$XM_DIR"

cat > /etc/systemd/system/xiaomusic.service << EOF
[Unit]
Description=Xiaomusic Music Service
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/xiaomusic -c ${XM_DIR}/config.json
Restart=on-failure
RestartSec=5
WorkingDirectory=${XM_DIR}
LimitNOFILE=4096
MemoryMax=96M

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable xiaomusic
systemctl start xiaomusic

echo "[✓] xiaomusic service 已安装并启动 (数据目录: ${XM_DIR})"
