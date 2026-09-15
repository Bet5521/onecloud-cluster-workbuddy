#!/bin/bash
# OneCloud Cluster Panel - systemd 服务
# 复制到 /etc/systemd/system/ 后启用

PANEL_DIR="$(cd "$(dirname "$0")" && pwd)"
NODE_NAME="${NODE_NAME:-wk-edge-01}"
PANEL_SERVICE="${PANEL_DIR}/config.json"

# 监听参数可由环境变量注入 (init/init.sh 会导出), 未提供时保持原默认值
PANEL_HOST="${PANEL_HOST:-0.0.0.0}"
PANEL_PORT="${PANEL_PORT:-9000}"

# ---- 监听地址校验 ----
# 这里写入的值会被 app.py 当作绑定地址: 填成网段地址 (192.168.1.0) 或回环网段的
# 网络地址 (127.0.0.0) 不会当场报错, 但 systemd 启动时必然失败
# (Cannot assign requested address)。宁可现在拒绝, 也不留下一个起不来的服务。
# 校验逻辑优先复用 scripts/lib-panel-host.sh, 库不在时用内联兜底。
_LIB_PANEL_HOST="$(cd "${PANEL_DIR}/.." 2>/dev/null && pwd)/scripts/lib-panel-host.sh"
if [ -f "$_LIB_PANEL_HOST" ]; then
    . "$_LIB_PANEL_HOST"
fi

if declare -F panel_host_check >/dev/null 2>&1; then
    if ! panel_host_check "$PANEL_HOST"; then
        echo "[ERROR] PANEL_HOST=${PANEL_HOST} 不可用: ${PANEL_HOST_REASON}" >&2
        [ -n "$PANEL_HOST_SUGGEST" ] && echo "        建议改用: ${PANEL_HOST_SUGGEST}" >&2
        exit 1
    fi
else
    case "$PANEL_HOST" in
        127.0.0.0|127.*.0)
            echo "[ERROR] PANEL_HOST=${PANEL_HOST} 是回环网段的网络地址, 不是可用监听地址" >&2
            echo "        仅本机访问请用 127.0.0.1; 同网段访问请填本机局域网地址" >&2
            exit 1 ;;
        *.*.*.0)
            if [ "$PANEL_HOST" != "0.0.0.0" ]; then
                echo "[ERROR] PANEL_HOST=${PANEL_HOST} 是网络地址 (整个网段), 请填某台主机的地址" >&2
                exit 1
            fi ;;
    esac
fi

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
echo "    监听: ${PANEL_HOST}:${PANEL_PORT}"
# 0.0.0.0 / 127.0.0.1 都不是可直接访问的地址, 按类型给出真实入口
if declare -F panel_detect_local_ipv4 >/dev/null 2>&1; then
    _IP_LIST="$(panel_detect_local_ipv4)"
else
    _IP_LIST="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v '^$')"
fi
case "$PANEL_HOST" in
    0.0.0.0)
        if [ -n "$_IP_LIST" ]; then
            for _ip in $_IP_LIST; do
                echo "    访问: http://${_ip}:${PANEL_PORT}"
            done
        else
            echo "    访问: http://<本机IP>:${PANEL_PORT}"
        fi
        ;;
    127.0.0.1)
        echo "    访问: http://127.0.0.1:${PANEL_PORT} (仅本机)"
        echo "    远端: ssh -L ${PANEL_PORT}:127.0.0.1:${PANEL_PORT} <用户>@<节点IP> 后访问本机同端口"
        ;;
    *)
        echo "    访问: http://${PANEL_HOST}:${PANEL_PORT}"
        ;;
esac

if ! systemctl is-active --quiet onecloud-panel; then
    echo "[WARN] onecloud-panel 未处于运行状态, 请查看: systemctl status onecloud-panel" >&2
    echo "       若为地址不可用, 请确认 PANEL_HOST=${PANEL_HOST} 是本机某张网卡的地址" >&2
fi
