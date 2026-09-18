#!/bin/bash
# Clash/mihomo systemd 服务文件生成脚本
#
# 数据目录不写死: 由本脚本在安装时解析本节点实际数据根
# (SD 卡挂载点, 无卡回退 /opt/onecloud), 再渲染进 systemd 单元。
set -e

# ---- 解析本节点数据根 (与 scripts/lib-services.sh 的约定一致) ----
resolve_data_root_here() {
    # 1. 节点安装事实 (由 bootstrap.sh 写入 /etc/onecloud/install.conf)
    if [ -r /etc/onecloud/install.conf ]; then
        local dr
        dr="$(sed -n 's/^DATA_ROOT=//p' /etc/onecloud/install.conf | head -n1)"
        [ -n "$dr" ] && { printf '%s' "$dr"; return 0; }
    fi
    # 2. 常见 SD 挂载点
    local m
    for m in /mnt/sd /mnt/data /media/sd; do
        if mountpoint -q "$m" 2>/dev/null && [ -w "$m" ]; then
            printf '%s' "$m"; return 0
        fi
    done
    # 3. 回退
    printf '%s' "${ONECLOUD_REMOTE_DATA_ROOT:-/opt/onecloud}"
}

# 占位符优先: 若上游已渲染 __DATA_ROOT__, 直接采用
DATA_ROOT="${__DATA_ROOT__:-}"
case "$DATA_ROOT" in
    ""|"__DATA_ROOT__") DATA_ROOT="$(resolve_data_root_here)" ;;
esac
NODE_NAME="${__NODE_NAME__:-}"
case "$NODE_NAME" in
    ""|"__NODE_NAME__") NODE_NAME="$(hostname | sed 's/\..*//')" ;;
esac
CLASH_DIR="${DATA_ROOT}/srv/${NODE_NAME}/clash"

mkdir -p "$CLASH_DIR"

cat > /etc/systemd/system/mihomo.service << EOF
[Unit]
Description=Mihomo Proxy Service
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/mihomo -d ${CLASH_DIR}
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

echo "[✓] mihomo service 已安装并启动 (数据目录: ${CLASH_DIR})"
