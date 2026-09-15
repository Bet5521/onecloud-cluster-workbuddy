#!/bin/bash
# ============================================================
# OneCloud Cluster Panel - systemd 服务安装
#
# 用法: sudo bash install-service.sh [选项]
#
#   --host ADDR       面板**监听**地址 (绑到哪张网卡, 默认 0.0.0.0 = 全部网卡)
#   --port PORT       面板**监听**端口 (默认 9000)
#   --url-host HOST   面板**访问**地址 (面板 IP 或域名, 用于生成访问入口)
#   --url-port PORT   面板**访问**端口 (默认与监听端口一致)
#   -y, --yes         不询问, 全部用环境变量/默认值 (自动化场景)
#   -h, --help        显示帮助
#
# 三个"地址/端口"不是一回事, 这里刻意分开:
#   监听地址 —— 进程真正 bind 的地址, 必须是本机某张网卡的地址或 0.0.0.0
#   监听端口 —— 进程真正 bind 的端口
#   访问地址 —— 浏览器里敲的"面板 IP/域名 + 端口"; 与监听值不同是常态
#               (SSH 端口转发、Nginx 反代、路由器端口映射都会让二者不同)
#
# 交互: 未通过参数/环境变量提供且终端可用时逐个询问, 直接回车 = 默认值。
#       init/init.sh 用 `sudo env PANEL_HOST=... PANEL_PORT=... bash install-service.sh --yes`
#       调用, 因此不会被重复询问。
#
# 落盘位置:
#   /etc/systemd/system/onecloud-panel.service   服务定义 (监听参数内联)
#   /etc/onecloud/panel.env                      监听/访问参数 (600, 合并写入)
# 账号密码由 init/init.sh 决定, 本脚本只更新自己负责的四个键, 其余行原样保留。
# ============================================================

PANEL_DIR="$(cd "$(dirname "$0")" && pwd)"
NODE_NAME="${NODE_NAME:-wk-edge-01}"
PANEL_SERVICE="${PANEL_DIR}/config.json"
PANEL_ENV_FILE="${ONECLOUD_PANEL_ENV_FILE:-/etc/onecloud/panel.env}"
PANEL_UNIT="${ONECLOUD_PANEL_UNIT:-/etc/systemd/system/onecloud-panel.service}"

usage() {
    cat << EOF
用法: sudo bash $0 [选项]

选项:
  --host ADDR       面板监听地址 (默认 0.0.0.0 = 全部网卡; 只本机可填 127.0.0.1)
  --port PORT       面板监听端口 (默认 9000)
  --url-host HOST   面板访问地址: 面板 IP 或域名 (默认自动探测本机地址)
  --url-port PORT   面板访问端口 (默认与监听端口一致; 前面有反代/NAT 时填它)
  -y, --yes         不询问, 全部取环境变量/默认值
  -h, --help        显示帮助

环境变量 (与选项等价, 选项优先):
  PANEL_HOST / PANEL_PORT / PANEL_URL_HOST / PANEL_URL_PORT

示例:
  sudo bash $0                                     # 交互式确认三个参数
  sudo bash $0 --host 192.168.1.101 --port 9000    # 只监听本机局域网地址
  sudo bash $0 --host 127.0.0.1 --port 9000 --url-port 19000
                                                   # 仅本机 + SSH 转发到 19000
  sudo bash $0 -y                                  # 全部默认, 不询问
EOF
}

# ---- 参数解析 (参数 > 环境变量 > 默认) ----
ARG_HOST=""; ARG_PORT=""; ARG_URL_HOST=""; ARG_URL_PORT=""
ENV_HOST="${PANEL_HOST:-}"; ENV_PORT="${PANEL_PORT:-}"
ENV_URL_HOST="${PANEL_URL_HOST:-}"; ENV_URL_PORT="${PANEL_URL_PORT:-}"
ASSUME_YES=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host)      ARG_HOST="${2:-}";     shift 2 ;;
        --port)      ARG_PORT="${2:-}";     shift 2 ;;
        --url-host)  ARG_URL_HOST="${2:-}"; shift 2 ;;
        --url-port)  ARG_URL_PORT="${2:-}"; shift 2 ;;
        -y|--yes)    ASSUME_YES=true;       shift ;;
        -h|--help)   usage; exit 0 ;;
        *) echo "[ERROR] 未知选项: $1" >&2; usage >&2; exit 2 ;;
    esac
done

# ---- 默认值 (占位, 下面按 参数 > 环境变量 > 默认 的优先级覆盖) ----
PANEL_HOST="${PANEL_HOST:-0.0.0.0}"
PANEL_PORT="${PANEL_PORT:-9000}"

# ---- 校验逻辑优先复用 scripts/lib-panel-host.sh, 库不在时用内联兜底 ----
_LIB_PANEL_HOST="$(cd "${PANEL_DIR}/.." 2>/dev/null && pwd)/scripts/lib-panel-host.sh"
if [ -f "$_LIB_PANEL_HOST" ]; then
    # shellcheck source=lib-panel-host.sh
    . "$_LIB_PANEL_HOST"
fi

# 端口: 1-65535 的数字
check_port() {
    local p="${1:-}"
    case "$p" in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ "$p" -ge 1 ] && [ "$p" -le 65535 ]
}

# 监听地址: 复用 lib-panel-host.sh 的判定 (拦网段地址 / 回环网段网络地址等)
check_listen_host() {
    local ip="${1:-}"
    if declare -F panel_host_check >/dev/null 2>&1; then
        panel_host_check "$ip"
        return $?
    fi
    case "$ip" in
        127.0.0.0|127.*.0)
            PANEL_HOST_REASON="${ip} 是回环网段的网络地址, 不是可用监听地址"
            PANEL_HOST_SUGGEST="127.0.0.1"
            return 1 ;;
        *.*.*.0)
            if [ "$ip" != "0.0.0.0" ]; then
                PANEL_HOST_REASON="${ip} 是网络地址 (整个网段), 请填某台主机的地址"
                PANEL_HOST_SUGGEST="$(hostname -I 2>/dev/null | awk '{print $1}')"
                return 1
            fi ;;
    esac
    printf '%s\n' "$ip" | awk -F. 'NF != 4 { exit 1 }
        { for (i = 1; i <= 4; i++) { if ($i !~ /^[0-9]+$/ || $i + 0 > 255) exit 1 } }' || {
        PANEL_HOST_REASON="不是合法的 IPv4 地址"
        return 1
    }
    return 0
}

# 访问地址: 允许 IPv4 或主机名/域名 (它只是给人看的入口, 不参与 bind)
check_url_host() {
    local h="${1:-}"
    [ -n "$h" ] || return 1
    if printf '%s' "$h" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
        printf '%s\n' "$h" | awk -F. 'NF != 4 { exit 1 }
            { for (i = 1; i <= 4; i++) { if ($i + 0 > 255) exit 1 } }' || return 1
        return 0
    fi
    printf '%s' "$h" | grep -qE '^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$' \
        || printf '%s' "$h" | grep -qE '^[A-Za-z0-9]$'
}

# 探测本机第一个可用 IPv4 (用于给"访问地址"填空)
detect_first_ip() {
    if declare -F panel_detect_local_ipv4 >/dev/null 2>&1; then
        panel_detect_local_ipv4 | head -n 1
        return 0
    fi
    hostname -I 2>/dev/null | tr ' ' '\n' | grep -vE '^$|^127\.' | head -n 1
}

INTERACTIVE=true
if [ "$ASSUME_YES" = true ]; then
    INTERACTIVE=false
elif [ -n "${ONECLOUD_PANEL_TTY:-}" ]; then
    # 与 bootstrap.sh 的 ONECLOUD_BOOTSTRAP_TTY 同一约定: 便于非交互环境演练交互分支
    if [ "$ONECLOUD_PANEL_TTY" = "1" ]; then
        INTERACTIVE=true
    else
        INTERACTIVE=false
    fi
elif [ ! -t 0 ]; then
    INTERACTIVE=false
fi

echo ""
echo "OneCloud Panel 服务安装 (监听地址 / 监听端口 / 访问地址)"
echo "--------------------------------------------------------"

# ---- 监听地址: 命令行 > 环境变量 > 默认 0.0.0.0 ----
PANEL_HOST="${ARG_HOST:-${ENV_HOST:-$PANEL_HOST}}"
if [ "$INTERACTIVE" = true ] && [ -z "$ARG_HOST" ] && [ -z "$ENV_HOST" ]; then
    _lan_ip="$(detect_first_ip)"
    echo "  监听地址决定「谁能连上面板」:"
    echo "    0.0.0.0      全部网卡 (含 WireGuard / 外网, 暴露面最大)"
    echo "    ${_lan_ip:-<本机IP>}   仅本机局域网地址 (同网段可访问, 推荐)"
    echo "    127.0.0.1    仅本机 (远端访问需 SSH 端口转发)"
    while :; do
        read -r -p "  面板监听地址 [回车=0.0.0.0]: " PANEL_HOST || true
        [ -n "$PANEL_HOST" ] || PANEL_HOST="0.0.0.0"
        if check_listen_host "$PANEL_HOST"; then
            break
        fi
        echo "  [ERROR] ${PANEL_HOST_REASON}" >&2
        [ -n "$PANEL_HOST_SUGGEST" ] && echo "          建议改用: ${PANEL_HOST_SUGGEST}" >&2
    done
fi

# ---- 监听端口: 命令行 > 环境变量 > 默认 9000 ----
PANEL_PORT="${ARG_PORT:-${ENV_PORT:-$PANEL_PORT}}"
if [ "$INTERACTIVE" = true ] && [ -z "$ARG_PORT" ] && [ -z "$ENV_PORT" ]; then
    while :; do
        read -r -p "  面板监听端口 [回车=9000]: " PANEL_PORT || true
        [ -n "$PANEL_PORT" ] || PANEL_PORT="9000"
        check_port "$PANEL_PORT" && break
        echo "  [ERROR] 端口应为 1-65535 的数字" >&2
    done
fi

# ---- 访问地址 / 访问端口 (只影响回显与 panel.env, 不影响 bind) ----
PANEL_URL_HOST="${ARG_URL_HOST:-${ENV_URL_HOST:-}}"
if [ -z "$PANEL_URL_HOST" ] && [ "$INTERACTIVE" = true ]; then
    _default_url_host="$(detect_first_ip)"
    read -r -p "  面板访问地址 (面板 IP 或域名) [回车=${_default_url_host:-跳过}]: " PANEL_URL_HOST || true
fi
[ -n "$PANEL_URL_HOST" ] || PANEL_URL_HOST="$(detect_first_ip)"

PANEL_URL_PORT="${ARG_URL_PORT:-${ENV_URL_PORT:-}}"
if [ -z "$PANEL_URL_PORT" ]; then
    if [ "$INTERACTIVE" = true ] && [ -n "$PANEL_URL_HOST" ]; then
        read -r -p "  面板访问端口 (对外访问用; 经反代/NAT 时填它) [回车=${PANEL_PORT}]: " PANEL_URL_PORT || true
    fi
    [ -n "$PANEL_URL_PORT" ] || PANEL_URL_PORT="$PANEL_PORT"
fi

# ---- 统一校验 (含 env/参数路径, 非法值直接拒绝, 不留下起不来的服务) ----
# 这里写入的值会被 app.py 当作绑定地址: 填成网段地址 (192.168.1.0) 或回环网段的
# 网络地址 (127.0.0.0) 不会当场报错, 但 systemd 启动时必然失败
# (Cannot assign requested address)。宁可现在拒绝。
if ! check_listen_host "$PANEL_HOST"; then
    echo "[ERROR] PANEL_HOST=${PANEL_HOST} 不可用: ${PANEL_HOST_REASON}" >&2
    [ -n "$PANEL_HOST_SUGGEST" ] && echo "        建议改用: ${PANEL_HOST_SUGGEST}" >&2
    exit 1
fi
if ! check_port "$PANEL_PORT"; then
    echo "[ERROR] PANEL_PORT=${PANEL_PORT} 不是合法端口 (1-65535)" >&2
    exit 1
fi
if [ -n "$PANEL_URL_HOST" ] && ! check_url_host "$PANEL_URL_HOST"; then
    echo "[ERROR] 面板访问地址 ${PANEL_URL_HOST} 不合法 (需 IPv4 或域名)" >&2
    exit 1
fi
if ! check_port "$PANEL_URL_PORT"; then
    echo "[ERROR] 面板访问端口 ${PANEL_URL_PORT} 不是合法端口 (1-65535)" >&2
    exit 1
fi

# ---- 落盘: 合并写 panel.env (账号密码等其它键不能被冲掉) ----
panel_env_set() {
    local key="$1" val="$2" dir
    dir="$(dirname "$PANEL_ENV_FILE")"
    mkdir -p "$dir" 2>/dev/null || true
    if [ -f "$PANEL_ENV_FILE" ]; then
        grep -v "^${key}=" "$PANEL_ENV_FILE" > "${PANEL_ENV_FILE}.tmp" 2>/dev/null || true
    else
        : > "${PANEL_ENV_FILE}.tmp"
    fi
    printf '%s=%s\n' "$key" "$val" >> "${PANEL_ENV_FILE}.tmp"
    mv "${PANEL_ENV_FILE}.tmp" "$PANEL_ENV_FILE" || return 1
    chmod 600 "$PANEL_ENV_FILE" 2>/dev/null || true
    return 0
}

if [ -n "$PANEL_ENV_FILE" ]; then
    _env_written=false
    if panel_env_set PANEL_HOST "$PANEL_HOST" \
       && panel_env_set PANEL_PORT "$PANEL_PORT"; then
        _env_written=true
    fi
    # 访问地址为空 (探测不到本机 IP) 时不写空键, 免得后续读到空值
    if [ -n "$PANEL_URL_HOST" ]; then
        panel_env_set PANEL_URL_HOST "$PANEL_URL_HOST" \
            && panel_env_set PANEL_URL_PORT "$PANEL_URL_PORT"
    fi
    if [ "$_env_written" = true ]; then
        echo "[✓] 监听/访问参数已写入 ${PANEL_ENV_FILE} (600)"
    else
        echo "[WARN] 未能写入 ${PANEL_ENV_FILE} (权限不足?), 服务仍按 unit 内联值启动" >&2
    fi
fi

# ---- 写 systemd unit (监听参数同时内联一份, 文件缺失也能起来) ----
cat > "$PANEL_UNIT" << EOF
[Unit]
Description=OneCloud Cluster Control Panel
After=network.target

[Service]
Type=simple
WorkingDirectory=${PANEL_DIR}
ExecStart=/usr/bin/python3 ${PANEL_DIR}/app.py
Restart=on-failure
RestartSec=5
EnvironmentFile=-${PANEL_ENV_FILE}
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
if declare -F panel_host_desc >/dev/null 2>&1; then
    echo "    监听: ${PANEL_HOST}:${PANEL_PORT}  ($(panel_host_desc "$PANEL_HOST"))"
else
    echo "    监听: ${PANEL_HOST}:${PANEL_PORT}"
fi

# 访问入口: 优先用"访问地址/端口", 没有才按监听类型推导
if [ -n "$PANEL_URL_HOST" ]; then
    echo "    访问: http://${PANEL_URL_HOST}:${PANEL_URL_PORT}"
    if [ "$PANEL_URL_PORT" != "$PANEL_PORT" ]; then
        echo "          (对外端口 ${PANEL_URL_PORT} 经反代/NAT 转发到本机 ${PANEL_PORT})"
    fi
    if [ "$PANEL_URL_HOST" != "$PANEL_HOST" ] && [ "$PANEL_HOST" = "127.0.0.1" ]; then
        echo "          监听在 127.0.0.1, 该入口需经 SSH 端口转发才可达:"
        echo "          ssh -L ${PANEL_URL_PORT}:127.0.0.1:${PANEL_PORT} <用户>@${PANEL_URL_HOST}"
    fi
else
    case "$PANEL_HOST" in
        0.0.0.0)
            for _ip in $(if declare -F panel_detect_local_ipv4 >/dev/null 2>&1; then
                             panel_detect_local_ipv4
                         else
                             hostname -I 2>/dev/null
                         fi); do
                echo "    访问: http://${_ip}:${PANEL_PORT}"
            done
            ;;
        127.0.0.1)
            echo "    访问: http://127.0.0.1:${PANEL_PORT} (仅本机)"
            echo "    远端: ssh -L ${PANEL_PORT}:127.0.0.1:${PANEL_PORT} <用户>@<节点IP> 后访问本机同端口"
            ;;
        *)
            echo "    访问: http://${PANEL_HOST}:${PANEL_PORT}"
            ;;
    esac
fi

if ! systemctl is-active --quiet onecloud-panel; then
    echo "[WARN] onecloud-panel 未处于运行状态, 请查看: systemctl status onecloud-panel" >&2
    echo "       若为地址不可用, 请确认 PANEL_HOST=${PANEL_HOST} 是本机某张网卡的地址" >&2
    exit 1
fi
