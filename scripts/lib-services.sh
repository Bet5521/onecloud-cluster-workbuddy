#!/bin/bash
# ============================================================
# 服务安装态 / 组网模式 / 数据根 单一真相库 (lib-services.sh)
#
# 解决的根因: "这个组件装没装"曾经散落在各脚本里各自判断
#   - health-check.sh 无条件检查 WireGuard       -> 不装 WG 就误报失败
#   - panel/app.py 只报 running, 不报 installed  -> 没装显示成"离线"
#   - 数据根有 /mnt/sd/srv /x、/mnt/sd/x、<DATA_ROOT>/srv/x 三套写法
# 本库把这三件事收敛成单点计算, 任何脚本都不得再自行拼逻辑。
#
# 关键语义 (实现与调用方都必须守住):
#   installed = "应当安装" (清单声明 + 组网模式判定 + 安装方式), 不是"探测到进程"。
#               运行态由 running 单独表达。二者组合三态:
#                 installed=false                      -> 未安装
#                 installed=true  & running=false      -> 已安装但停止
#                 installed=true  & running=true       -> 运行中
#
# 依赖: 只 source lib-nodes.sh (不重复实现清单解析)
# 约定: 不 set -e (由调用方控制); 不定义 log_* (避免与调用方冲突)
#
# 用法:
#   source "$(dirname "$0")/lib-services.sh"
#   network_mode                  # -> lan | wireguard | mixed
#   wg_enabled                    # -> 0 | 1
#   service_installed wk-edge-01 wireguard   # -> 0 | 1  (0=未安装)
#   service_data_dir wk-edge-01 wireguard    # -> /mnt/sd/srv/wk-edge-01/wireguard
# ============================================================

# 防止重复 source
if [ -n "${_LIB_SERVICES_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
_LIB_SERVICES_LOADED=1

# 只依赖 lib-nodes.sh: 清单解析/节点取值全部复用, 不重复实现
_LIB_SERVICES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -z "${_LIB_NODES_LOADED:-}" ]; then
    # shellcheck source=lib-nodes.sh
    . "${_LIB_SERVICES_DIR}/lib-nodes.sh"
fi

# ----------------------------------------------------------------------------
# 布尔归一: 把 true/yes/1/on 之类的写法统一成 0/1
# 本库对外一律用 0=假 1=真 (与 shell 退出码语义相反, 故不用 return 传递)
# ----------------------------------------------------------------------------
_svc_bool() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on)  printf '1' ;;
        *)              printf '0' ;;
    esac
}

# ----------------------------------------------------------------------------
# 1. 组网模式
#    auto      按清单推导: 有 wireguard 服务且 default_enabled -> mixed, 否则 lan
#    wireguard 仅 WireGuard 组网
#    lan       仅局域网直连 (强制不启用 WireGuard)
#    mixed     混合 (LAN 直连优先, 跨网段走 WireGuard)
# 非法值回退 auto 并提示 (不中断, 避免一个笔误卡死全部脚本)
# ----------------------------------------------------------------------------
network_mode() {
    local raw
    raw="$(printf '%s' "$(network_mode_raw)" | tr '[:upper:]' '[:lower:]')"
    case "$raw" in
        wireguard|lan|mixed)
            printf '%s' "$raw"
            return 0
            ;;
        auto|"")
            if [ "$(_svc_auto_has_wg)" = "1" ]; then
                printf 'mixed'
            else
                printf 'lan'
            fi
            return 0
            ;;
        *)
            echo "[WARN] network.mode='$raw' 不是合法取值 (auto|wireguard|lan|mixed), 按 auto 处理" >&2
            if [ "$(_svc_auto_has_wg)" = "1" ]; then printf 'mixed'; else printf 'lan'; fi
            return 0
            ;;
    esac
}

# 内部: auto 模式的推导依据 —— 是否存在"默认启用"的 wireguard 服务
_svc_auto_has_wg() {
    local owner
    owner="$(node_of_service wireguard 2>/dev/null || true)"
    [ -z "$owner" ] && { printf '0'; return 0; }
    local de
    de="$(services_default_enabled wireguard)"
    _svc_bool "$de"
}

# ----------------------------------------------------------------------------
# 2. 全局 WireGuard 开关
#    mode=lan 时**强制** 0 —— 即使清单里还留着 wireguard 服务也视作未安装。
#    这样用户切模式不必同时改两处, 避免"改了 mode 忘了删服务"的中间态。
# ----------------------------------------------------------------------------
wg_enabled() {
    # 结果缓存: service_installed -> wg_enabled_on -> wg_enabled 这条链在渲染一份
    # 配置时每个 (节点,服务) 都会走一遍, 不缓存等于反复重建同一份只读事实。
    # 依赖的清单/模式在单次进程内不变, 缓存安全。
    if [ -n "${_WG_ENABLED_CACHE:-}" ]; then
        printf '%s' "$_WG_ENABLED_CACHE"
        return 0
    fi
    local _r="0" n
    if [ "$(network_mode)" != "lan" ]; then
        for n in $(node_names); do
            if [ "$(node_has_service "$n" wireguard)" = "1" ]; then
                _r="1"
                break
            fi
        done
    fi
    _WG_ENABLED_CACHE="$_r"
    printf '%s' "$_r"
}

# 该节点是否装 WireGuard (考虑全局开关)
wg_enabled_on() {
    local n="${1:-}" g
    [ -z "$n" ] && { printf '0'; return 0; }
    if ! g="$(wg_enabled)"; then
        g="0"
    fi
    [ "$g" = "1" ] || { printf '0'; return 0; }
    node_has_service "$n" wireguard
}

# ----------------------------------------------------------------------------
# 3. 服务声明 / 安装方式
# ----------------------------------------------------------------------------
node_has_service() {   # node_has_service <节点名> <服务名>
    local n="$1" want="$2" s
    # 节点→服务集合缓存: 本函数被 service_installed 反复调用 (面板状态表一次
    # ~40 次), 每次都 `$(node_services)` 起一个子 shell 重新解析 nodes.yaml。
    # 单次进程内清单不变, 缓存安全。
    local _ck="_HAS_SVC_CACHE_${n//[^A-Za-z0-9]/_}"
    if [ -z "${!_ck:-}" ]; then
        local _list=""
        for s in $(node_services "$n"); do
            _list="${_list} ${s}"
        done
        printf -v "$_ck" '%s' "$_list"
    fi
    # 纯字符串匹配取代 `for s in ${!_ck}` 循环: 前者 18 次调用几乎零成本。
    # 两侧加空格保证整词匹配 (列表已被规整为 " a b c" 形式)。
    case " ${!_ck} " in
        *" ${want} "*) printf '1' ;;
        *)             printf '0' ;;
    esac
}

# 是否可选组件 (未声明时默认 false = 必装)
#   注: 直接取 service_field, 不再用 `local v="$(service_field ...)"`。后者每次
#   多起一个子 shell, 而本函数在渲染一份面板配置时会被调用 18 次 —— 在
#   Git Bash on Windows 上是一秒钟量级的纯浪费。
services_optional() {   # services_optional <服务名>
    local v
    if ! v="$(service_field "$1" optional 2>/dev/null)"; then
        v=""
    fi
    _svc_bool "${v:-false}"
}

# 是否默认安装 (未声明时默认 true)
services_default_enabled() {   # services_default_enabled <服务名>
    local v
    if ! v="$(service_field "$1" default_enabled 2>/dev/null)"; then
        v=""
    fi
    _svc_bool "${v:-true}"
}

# 安装方式: 空(有自动安装实现) | manual(需人工) | external(外部管理)
services_install_mode() {   # services_install_mode <服务名>
    local v
    if ! v="$(service_field "$1" install 2>/dev/null)"; then
        v=""
    fi
    case "${v,,}" in
        manual|external) printf '%s' "${v,,}" ;;
        *)               printf '' ;;
    esac
}

# 声明的网络能力 (如 wg-mesh); 无则空
services_provides() {   # services_provides <服务名>
    service_field "$1" provides 2>/dev/null || printf ''
}

# 主端口 (无则 0)
#
#   清单里两种写法都要认:
#     port: 8081                (原生服务, 单标量)
#     ports: ["51820:51820/udp"] (容器服务, 映射列表)
#   原先只查 port, 于是所有容器服务在主端口上是 0 —— 面板/健康检查据此
#   判断"该服务没有已知端口"是不对的。这里对 ports 列表取**第一条**的
#   宿主机侧端口作为主端口, 与 firewall-recommend.sh 的解析口径一致。
services_port() {   # services_port <服务名>
    local v
    # 端口结果按服务名缓存: `services_status_table` 会对**每个 (节点,服务) 组合**
    # 调一次本函数, 不缓存就是 ~40 次 awk 全量重解析 services.yaml。在 Git Bash
    # on Windows 上一次 fork 要 1~2 秒, 整体会从"瞬间"退化到 4 分钟量级。
    # 缓存键是服务名, 与节点/模式无关, 所以可安全复用。
    local _ck="_SVC_PORT_CACHE_${1//[^A-Za-z0-9]/_}"
    v="${!_ck:-}"
    if [ -z "$v" ]; then
        if ! v="$(service_field "$1" port 2>/dev/null)"; then
            v=""
        fi
        case "$v" in
            ''|*[!0-9]*) v="$(_svc_first_port "$1")" ;;
        esac
        case "$v" in
            ''|*[!0-9]*) v="0" ;;
        esac
        printf -v "$_ck" '%s' "$v"
    fi
    printf '%s' "$v"
}

# 内部: 取 services.yaml 中某服务 ports 列表第一条的宿主端口
#   _SVC_PORTMAP 已由 _svc_portmap_load 一次性解析为 "服务 端口" 表,
#   这里纯 bash 查表 —— 原实现每次 `printf | awk` 会 fork 一个管道, 而本
#   函数在渲染配置时被反复调用, 是 Git Bash 上最贵的几处之一。
_svc_first_port() {
    _svc_portmap_load
    # ${!array[@]} 在 set -u 下对"已声明但为空"的数组会报 unbound variable,
    # 必须先判空再展开 —— 否则所有容器服务的端口都会静默退化成 0。
    [ "${#_SVC_PORTMAP[@]}" -eq 0 ] && return 0
    local name p
    for name in "${!_SVC_PORTMAP[@]}"; do
        p="${_SVC_PORTMAP[$name]}"
        [ "$name" = "$1" ] && { printf '%s' "$p"; return 0; }
    done
    return 0
}

# 一次性把 services.yaml 的 ports 段解析进关联数组 (键=服务名, 值=宿主端口)
declare -A _SVC_PORTMAP=()
_SVC_PORTMAP_LOADED=""

_svc_portmap_load() {
    [ -n "$_SVC_PORTMAP_LOADED" ] && return 0
    _SVC_PORTMAP_LOADED=1
    local inpath="${LIB_NODES_PROJECT_DIR}/inventory/services.yaml"
    [ -f "$inpath" ] || return 0
    # 原实现对每个服务都重跑一遍 awk 扫全文件; 18 个服务 × 3 节点 = 54 次扫描,
    # 在 Git Bash on Windows 上足以把一次面板配置生成拖到分钟级。
    local name port
    while IFS=$'\t' read -r name port; do
        [ -n "$name" ] && [ -n "$port" ] && _SVC_PORTMAP["$name"]="$port"
    done < <(awk '
        BEGIN { cur = ""; inp = 0 }
        {
            line = $0; sub(/\r$/, "", line)
            if (line ~ /^[[:space:]]*#/ || line ~ /^[[:space:]]*$/) next
            if (line ~ /^[A-Za-z_]/) { cur = ""; inp = 0; next }
            if (line ~ /^  [A-Za-z_][A-Za-z0-9_-]*:/) {
                sub(/^  /, "", line); sub(/:.*$/, "", line)
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", line)
                cur = line; inp = 0; next
            }
            if (cur != "" && line ~ /^    ports:[[:space:]]*$/) { inp = 1; next }
            if (inp == 1 && cur != "" && line ~ /^    [A-Za-z_]/) { inp = 0; next }
            if (inp == 1 && cur != "" && line ~ /^      -[[:space:]]/) {
                v = line; sub(/^      -[[:space:]]*/, "", v)
                gsub(/"/, "", v); gsub(/[[:space:]]+$/, "", v)
                sub(/:.*$/, "", v)
                if (v != "" && v ~ /^[0-9]+$/) { printf "%s\t%s\n", cur, v; inp = 0 }
            }
        }' "$inpath")
}

# ----------------------------------------------------------------------------
# 4. 综合判定: 是否"应当安装"
#    清单没声明                   -> 0
#    WireGuard 被模式关闭          -> 0
#    安装方式为 manual/external   -> 0 (无自动安装实现)
#    否则                         -> 1
# ----------------------------------------------------------------------------
service_installed() {   # service_installed <节点名> <服务名>
    local n="${1:-}" s="${2:-}" h has
    [ -z "$n" ] || [ -z "$s" ] && { printf '0'; return 0; }

    # 全部走 "先赋值再判返回码" 形式, 避免 `$( )` 再套一层子 shell —— 本函数是
    # 面板配置渲染里最热的一处 (每个 节点×服务 组合一次), 每次省一个子 shell
    # 在 Git Bash on Windows 上就是省一秒。
    if ! h="$(node_has_service "$n" "$s")"; then
        h="0"
    fi
    [ "$h" = "1" ] || { printf '0'; return 0; }

    if [ "$s" = "wireguard" ]; then
        if ! has="$(wg_enabled_on "$n")"; then
            has="0"
        fi
        [ "$has" = "1" ] || { printf '0'; return 0; }
    fi

    local m
    if ! m="$(services_install_mode "$s")"; then
        m=""
    fi
    [ -n "$m" ] && { printf '0'; return 0; }

    printf '1'
}

# ----------------------------------------------------------------------------
# 5. 数据根 —— 全项目唯一入口
#    本机场景: 先 resolve_data_root 让 DATA_ROOT 有值
#    远端场景: 传 IP, 走 node_data_root (SSH 读 /etc/onecloud/install.conf)
#    两者都取不到时 node_data_root 自身会回退 ONECLOUD_REMOTE_DATA_ROOT 或 /mnt/sd
# ----------------------------------------------------------------------------
oc_data_root() {   # oc_data_root [远端IP]
    local ip="${1:-}"
    local local_root
    local_root="$(local_data_root 2>/dev/null || true)"
    if [ -n "$local_root" ]; then
        printf '%s' "$local_root"
        return 0
    fi
    if [ -n "$ip" ]; then
        node_data_root "$ip"
        return 0
    fi
    # 无 IP 无本机根: 取第一个节点的远端数据根 (控制端场景)
    ip="$(node_ip "$(node_names | head -n 1)" 2>/dev/null || true)"
    [ -n "$ip" ] || { printf '/mnt/sd'; return 0; }
    node_data_root "$ip"
}

# 非阻塞版数据根 —— 生成类脚本 (gen-panel-config / 配置渲染) 专用。
# 与 oc_data_root 的区别: 永不 SSH。理由见 lib-nodes.sh:static_data_root 注释。
oc_static_data_root() {
    local r
    r="$(local_data_root 2>/dev/null || true)"
    if [ -n "$r" ]; then
        printf '%s' "$r"
        return 0
    fi
    static_data_root
}

# 节点级数据目录: <DATA_ROOT>/srv/<完整节点名>
node_data_dir() {   # node_data_dir <节点名> [远端IP]
    local n="${1:-}" ip="${2:-}"
    [ -z "$ip" ] && ip="$(node_ip "$n" 2>/dev/null || true)"
    printf '%s' "$(oc_data_root "$ip")/srv/$n"
}

# 服务级数据目录: <DATA_ROOT>/srv/<完整节点名>/<服务名>
# 这是审计 C-3 的收敛点 —— 面板/安装脚本一律用它, 禁止再拼 /mnt/sd
service_data_dir() {   # service_data_dir <节点名> <服务名> [远端IP]
    local n="${1:-}" s="${2:-}" ip="${3:-}"
    [ -z "$n" ] && return 1
    printf '%s' "$(node_data_dir "$n" "$ip")/$s"
}

# ----------------------------------------------------------------------------
# 6. 连通性探测地址 (按模式取: lan 用 LAN IP, wireguard 用 wg IP, mixed 先 LAN)
#    取代各脚本里硬编码的 WG_HUB_IP=edge IP
# ----------------------------------------------------------------------------
probe_addr_for() {   # probe_addr_for <节点名>
    local n="${1:-}"
    [ -z "$n" ] && return 1
    case "$(network_mode)" in
        lan)        node_ip "$n" ;;
        wireguard)  node_wg_ip "$n" ;;
        *)          node_ip "$n" ;;   # mixed: 先 LAN, 调用方失败时自行回退 wg
    esac
}

# 回退地址 (mixed 模式下 LAN 不通时用)
probe_fallback_addr_for() {   # probe_fallback_addr_for <节点名>
    local n="${1:-}"
    [ -z "$n" ] && return 1
    case "$(network_mode)" in
        mixed) node_wg_ip "$n" ;;
        *)     return 1 ;;
    esac
}

# WireGuard Hub 节点 (从清单 wireguard 服务的 node 字段反查, 不硬编码角色)
wg_hub_node() {
    [ "$(wg_enabled)" = "1" ] || return 1
    node_of_service wireguard
}

wg_hub_ip() {
    local n
    n="$(wg_hub_node)" || return 1
    node_ip "$n"
}

# ----------------------------------------------------------------------------
# 7. 汇总表 (供 gen-panel-config.sh 消费)
#    输出 TSV: 节点名<TAB>服务名<TAB>installed<TAB>optional<TAB>install<TAB>port
# ----------------------------------------------------------------------------
services_status_table() {
    local n s inst opt imode port
    for n in $(node_names); do
        for s in $(node_services "$n"); do
            inst="$(service_installed "$n" "$s")"
            opt="$(services_optional "$s")"
            imode="$(services_install_mode "$s")"
            port="$(services_port "$s")"
            # imode 为空表示"自动安装"。**不要直接输出空字段** —— 调用方用
            # `IFS=$'\t' read` 解析时, bash 会把相邻制表符之间的空字段折叠掉,
            # 后面的 port 会左移一格顶上来 (实测: 安装方式列显示成端口号)。
            # 统一用 "-" 表示"无", 列数恒定 6 列。
            [ -n "$imode" ] || imode="-"
            printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$n" "$s" "$inst" "$opt" "$imode" "$port"
        done
    done
}

# ----------------------------------------------------------------------------
# 8. 模式展示名 (面板直接用, 不在前端拼字符串)
# ----------------------------------------------------------------------------
network_mode_label() {
    case "$(network_mode)" in
        wireguard) printf 'WireGuard 组网 (10.8.0.0/24)' ;;
        lan)       printf '局域网直连 (%s)' "${NET_LAN_SUBNET:-192.168.1.0/24}" ;;
        *)         printf '混合组网 (LAN 直连 + WireGuard Mesh)' ;;
    esac
}
