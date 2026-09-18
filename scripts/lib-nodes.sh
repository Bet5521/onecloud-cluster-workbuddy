#!/bin/bash
# ============================================================
# 节点清单库 (lib-nodes.sh)
#
# 所有运维脚本的单一数据源。节点 IP、主机名、WireGuard IP、
# 网关、域名等一律从这里读取, 禁止在业务脚本里硬编码。
#
# 用法:
#   source "$(dirname "$0")/lib-nodes.sh"
#   ip=$(node_ip wk-edge-01)
#   host=$(node_hostname wk-edge-01)
#   for n in $(node_names); do echo "$n -> $(node_ip "$n")"; done
#
# 自定义优先级 (高 -> 低):
#   1. 环境变量   ONECLOUD_<节点名大写>_IP / _HOSTNAME / _WG_IP / _ROLE
#                 ONECLOUD_GATEWAY / ONECLOUD_DOMAIN / ONECLOUD_DNS /
#                 ONECLOUD_LAN_SUBNET / ONECLOUD_WG_SUBNET / ONECLOUD_WG_PORT
#                 (节点名中的 - 和 . 需写成 _, 例: ONECLOUD_WK_EDGE_01_IP)
#   2. 覆盖文件   inventory/nodes.local.yaml  (不入库, 本地环境专用)
#   3. 默认清单   inventory/nodes.yaml        (入库, 集群默认拓扑)
# ============================================================

# 防止重复 source 时重复解析
if [ -n "${_LIB_NODES_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
_LIB_NODES_LOADED=1

# 该库被各脚本 source; 启用 nounset 以尽早暴露未定义变量
set -u

# 通用日志函数 (供调用方脚本复用)
log_info()  { echo -e "\033[0;32m[INFO]\033[0m $*"; }
log_error() { echo -e "\033[0;31m[ERROR]\033[0m $*"; }
log_warn()  { echo -e "\033[1;33m[WARN]\033[0m $*"; }

_LIB_NODES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_NODES_PROJECT_DIR="${LIB_NODES_PROJECT_DIR:-$(dirname "$_LIB_NODES_DIR")}"

NODES_YAML="${LIB_NODES_PROJECT_DIR}/inventory/nodes.yaml"
NODES_LOCAL_YAML="${LIB_NODES_PROJECT_DIR}/inventory/nodes.local.yaml"

# ------------------------------------------------------------
# 内部: 极简 YAML 解析 (只处理本项目 nodes.yaml 的固定结构)
# 输出两种行:
#   NODE|<name>|<field>|<value>
#   NET|<field>|<value>
# ------------------------------------------------------------
_parse_nodes_yaml() {
    local file="$1"
    [ -f "$file" ] || return 0
    awk '
    function trim(s) { gsub(/^[[:space:]]+|[[:space:]]+$/, "", s); gsub(/^"|"$/, "", s); return s }
    BEGIN { sec = ""; cur = "" }
    {
        line = $0
        sub(/\r$/, "", line)
        if (line ~ /^[[:space:]]*#/ || line ~ /^[[:space:]]*$/) next

        # 顶层段 (nodes: / network:)
        if (line ~ /^[A-Za-z_]/) {
            sec = line; sub(/:.*$/, "", sec); sec = trim(sec)
            cur = ""
            next
        }

        if (sec == "nodes") {
            # 新列表项: "  - name: wk-edge-01"
            if (line ~ /^[[:space:]]*-[[:space:]]*name:/) {
                sub(/^[[:space:]]*-[[:space:]]*/, "", line)
                p = index(line, ":")
                cur = trim(substr(line, p + 1))
                print "NODE|" cur "|name|" cur
                next
            }
            # 节点字段 (4 空格缩进; 6 空格是 storage 之类子块, 忽略)
            if (cur != "" && line ~ /^    [A-Za-z_]+:/) {
                sub(/^    /, "", line)
                p = index(line, ":")
                k = trim(substr(line, 1, p - 1))
                v = trim(substr(line, p + 1))
                # 允许捕获列表值 (如 services: [a, b, c]) 供 node_services 解析
                if (v != "") print "NODE|" cur "|" k "|" v
                next
            }
        }

        if (sec == "network") {
            if (line ~ /^  [A-Za-z_]+:/) {
                sub(/^  /, "", line)
                p = index(line, ":")
                k = trim(substr(line, 1, p - 1))
                v = trim(substr(line, p + 1))
                if (v != "" && v !~ /^\[/) print "NET|" k "|" v
            }
        }
    }' "$file"
}

# ------------------------------------------------------------
# 内部: 环境变量名规范化  wk-edge-01 -> WK_EDGE_01
# ------------------------------------------------------------
_env_key() {
    echo "$1" | tr '[:lower:]-.' '[:upper:]__'
}

# ------------------------------------------------------------
# 加载节点清单
# 结果:
#   ALL_NODES   数组, 格式 "name|hostname|ip|wg_ip|role"
#   NODE_NAMES  数组, 节点名列表
#   NET_*       网络参数
# ------------------------------------------------------------
load_nodes() {
    ALL_NODES=()
    NODE_NAMES=()
    declare -gA _NODE_FIELD=()

    local tmp
    tmp=$(_parse_nodes_yaml "$NODES_YAML"; _parse_nodes_yaml "$NODES_LOCAL_YAML")

    local line kind name field value
    while IFS='|' read -r kind name field value; do
        [ -z "${kind:-}" ] && continue
        if [ "$kind" = "NODE" ]; then
            _NODE_FIELD["${name}.${field}"]="$value"
        elif [ "$kind" = "NET" ]; then
            # NET 行只有 3 段: NET|<字段>|<值>
            _NODE_FIELD["__net__.${name}"]="$field"
        fi
    done <<< "$tmp"

    # 按出现顺序收集节点名 (保持 nodes.yaml 里的顺序, 本地文件的新节点追加在后)
    local seen="" n
    while IFS= read -r n; do
        [ -z "$n" ] && continue
        case " $seen " in *" $n "*) continue ;; esac
        seen="$seen $n"
        NODE_NAMES+=("$n")
    done < <(echo "$tmp" | awk -F'|' '$1=="NODE" && $3=="name" {print $2}')

    # 组装 ALL_NODES, 并套用环境变量覆盖
    for n in "${NODE_NAMES[@]}"; do
        local ip wg_ip hostname role ek ov
        ip="${_NODE_FIELD[${n}.ip]:-}"
        wg_ip="${_NODE_FIELD[${n}.wg_ip]:-}"
        role="${_NODE_FIELD[${n}.role]:-}"
        # hostname: 显式字段 > host 去掉域名后缀 > 节点名去掉 wk- 前缀
        hostname="${_NODE_FIELD[${n}.hostname]:-}"
        if [ -z "$hostname" ]; then
            hostname="${_NODE_FIELD[${n}.host]:-}"
            hostname="${hostname%%.*}"
        fi
        [ -z "$hostname" ] && hostname="${n#wk-}"

        ek=$(_env_key "$n")
        for pair in "IP:ip" "WG_IP:wg_ip" "HOSTNAME:hostname" "ROLE:role"; do
            ov="${pair%%:*}"
            ov="ONECLOUD_${ek}_${ov}"
            if [ -n "${!ov:-}" ]; then
                eval "${pair##*:}=\"\$$ov\""
            fi
        done

        _NODE_FIELD["${n}.ip"]="$ip"
        _NODE_FIELD["${n}.wg_ip"]="$wg_ip"
        _NODE_FIELD["${n}.hostname"]="$hostname"
        _NODE_FIELD["${n}.role"]="$role"
        ALL_NODES+=("${n}|${hostname}|${ip}|${wg_ip}|${role}")
    done

    # 网络参数 (环境变量优先)
    NET_GATEWAY="${ONECLOUD_GATEWAY:-${_NODE_FIELD[__net__.gateway]:-192.168.1.1}}"
    NET_DOMAIN="${ONECLOUD_DOMAIN:-${_NODE_FIELD[__net__.domain]:-yourdomain.com}}"
    NET_LAN_SUBNET="${ONECLOUD_LAN_SUBNET:-${_NODE_FIELD[__net__.lan_subnet]:-192.168.1.0/24}}"
    NET_WG_SUBNET="${ONECLOUD_WG_SUBNET:-${_NODE_FIELD[__net__.wg_subnet]:-10.8.0.0/24}}"
    NET_WG_PORT="${ONECLOUD_WG_PORT:-${_NODE_FIELD[__net__.wg_port]:-51820}}"
    # 允许 "dhcp"/"auto"/"none" 等标记值 (表示由 DHCP 自动获取);
    # 未配置时默认即为 dhcp, 不再强制兜底成 1.1.1.1
    NET_DNS="${ONECLOUD_DNS:-${_NODE_FIELD[__net__.dns]:-dhcp}}"

    # 组网模式 (auto|wireguard|lan|mixed)
    #   auto      按清单里 wireguard 服务是否存在且 default_enabled 推导
    #   wireguard 仅 WireGuard 组网   lan 仅局域网直连   mixed 混合
    # 取值与判定由 lib-services.sh 统一负责, 这里只做加载。
    NET_MODE="${ONECLOUD_NET_MODE:-${_NODE_FIELD[__net__.mode]:-auto}}"

    # LAN 掩码位数: 由 lan_subnet 推导, 例 192.168.1.0/24 -> 24
    NET_LAN_PREFIX="${NET_LAN_SUBNET##*/}"
    case "$NET_LAN_PREFIX" in
        ''|*[!0-9]*) NET_LAN_PREFIX="24" ;;
    esac

    return 0
}

# ------------------------------------------------------------
# 查询函数
# ------------------------------------------------------------
node_field() {   # node_field <节点名> <字段名>
    local n="$1" f="$2"
    if [ -z "${_NODE_FIELD[${n}.${f}]:-}" ] && [ -z "${_NODE_FIELD[${n}.name]:-}" ]; then
        return 1
    fi
    printf '%s' "${_NODE_FIELD[${n}.${f}]:-}"
}

node_ip()       { node_field "$1" ip; }
node_wg_ip()    { node_field "$1" wg_ip; }
node_hostname() { node_field "$1" hostname; }
node_role()     { node_field "$1" role; }
node_display_name() { node_field "$1" display_name; }
node_color()    { node_field "$1" color; }

# 输出某节点的服务列表, 每行一个 (解析 "services" 字段里的 [a, b, c])
node_services() {
    local raw val s
    raw="$(node_field "$1" services)"
    [ -z "$raw" ] && return 0
    # 去掉方括号后按逗号切分, 每个元素再去掉首尾空白。
    # 去空白不能用 `set -- $s`: 此时 IFS 已是逗号, 词分割按逗号走, 空格留着。
    # 也不能再 fork sed (面板状态表会遍历全部节点×服务, fork 数被放大几十倍),
    # 所以用 bash 内建的前后缀裁剪 —— 不产生子进程。
    val="${raw#[}"
    val="${val%]}"
    local IFS=','
    for s in $val; do
        s="${s#"${s%%[![:space:]]*}"}"   # 去头部空白
        s="${s%"${s##*[![:space:]]}"}"   # 去尾部空白
        [ -n "$s" ] && printf '%s\n' "$s"
    done
}

# 输出全部节点名, 每行一个 (便于 for 循环)
node_names() { local n; for n in "${NODE_NAMES[@]}"; do echo "$n"; done; }

# 解析节点名为有效标识, 支持简写 (edge-01 / wk-edge-01)
# 成功回显标准节点名, 失败返回 1
node_resolve() {
    local q="$1" n
    for n in "${NODE_NAMES[@]}"; do
        [ "$n" = "$q" ] && { echo "$n"; return 0; }
    done
    for n in "${NODE_NAMES[@]}"; do
        [ "${n#wk-}" = "$q" ] && { echo "$n"; return 0; }
        [ "$(node_hostname "$n")" = "$q" ] && { echo "$n"; return 0; }
    done
    return 1
}

# 反向查询: 由 IP 得到节点名
node_by_ip() {
    local ip="$1" n
    for n in "${NODE_NAMES[@]}"; do
        [ "$(node_ip "$n")" = "$ip" ] && { echo "$n"; return 0; }
    done
    return 1
}

# ------------------------------------------------------------
# 远程数据根目录: 节点上「服务数据实际所在」的根
#
# 背景: bootstrap.sh 会自适应选择数据根 —— SD 卡可用则挂载点, 否则回退
#       /opt/onecloud; 但控制端的 deploy/backup/restore/update 若一律按
#       /mnt/sd 读写, 在「无卡节点」上就会操作到不存在的目录。
#       节点在初始化时把结果写进 /etc/onecloud/install.conf (DATA_ROOT=...),
#       这里通过 SSH 读取它, 与节点事实保持一致。
#
# 用法: node_data_root <IP 或节点名>
# 取不到时回退 ONECLOUD_REMOTE_DATA_ROOT (默认 /mnt/sd, 兼容旧环境)。
# ------------------------------------------------------------
node_data_root() {
    local q="$1"
    local ip="$q" dr="" n
    # 允许直接传节点名
    if [ -n "${NODE_NAMES[*]:-}" ]; then
        for n in "${NODE_NAMES[@]}"; do
            [ "$n" = "$q" ] && { ip="$(node_ip "$n")"; break; }
        done
    fi
    if [ -n "$ip" ] && command -v ssh >/dev/null 2>&1; then
        dr="$(ssh -o ConnectTimeout=4 -o BatchMode=yes "root@${ip}" \
              "sed -n 's/^DATA_ROOT=//p' /etc/onecloud/install.conf 2>/dev/null | head -1" \
              2>/dev/null || true)"
        case "$dr" in
            /*) printf '%s' "$dr"; return 0 ;;
        esac
    fi
    printf '%s' "${ONECLOUD_REMOTE_DATA_ROOT:-/mnt/sd}"
}


# 按角色取节点名 (找不到时回退到按关键字匹配节点名)
#   node_name_by_role edge-gateway  ->  wk-edge-01
node_name_by_role() {
    local want=$1 n
    for n in $(node_names); do
        [ "$(node_role "$n")" = "$want" ] && { echo "$n"; return 0; }
    done
    # 回退: 角色名首段匹配节点名, 例 edge-gateway -> wk-edge-01
    local key="${want%%-*}"
    for n in $(node_names); do
        case "$n" in *"$key"*) echo "$n"; return 0 ;; esac
    done
    return 1
}

# 按角色取节点 IP
node_ip_by_role() {
    local n
    n="$(node_name_by_role "$1")" || return 1
    node_ip "$n"
}

# ------------------------------------------------------------
# 服务 -> 节点 映射 (解析 inventory/services.yaml)
#   node_of_service homeassistant   ->  wk-iot-02
# ------------------------------------------------------------
_parse_services_yaml() {
    local file="${LIB_NODES_PROJECT_DIR}/inventory/services.yaml"
    [ -f "$file" ] || return 0
    awk '
    BEGIN { cur = "" }
    {
        line = $0
        sub(/\r$/, "", line)
        if (line ~ /^[[:space:]]*#/ || line ~ /^[[:space:]]*$/) next
        if (line ~ /^[A-Za-z_]/) { cur = ""; next }
        # 服务名: 2 空格缩进的 "xxx:"
        if (line ~ /^  [A-Za-z_][A-Za-z0-9_-]*:/) {
            sub(/^  /, "", line); sub(/:.*$/, "", line)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", line)
            cur = line
            next
        }
        # 服务的 node 字段: 4 空格缩进
        if (cur != "" && line ~ /^    node:/) {
            sub(/^    node:/, "", line)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", line)
            print cur "|" line
            cur = ""
        }
    }' "$file"
}

node_of_service() {   # node_of_service <服务名>
    local svc="$1" line name owner
    while IFS='|' read -r name owner; do
        [ "$name" = "$svc" ] && { echo "$owner"; return 0; }
    done < <(_parse_services_yaml)
    return 1
}

# 列出全部服务名
service_names() {
    local name owner
    while IFS='|' read -r name owner; do
        echo "$name"
    done < <(_parse_services_yaml)
}

# 要求节点清单非空, 否则报错退出 (各脚本入口调用)
# ------------------------------------------------------------
# 服务字段查询 (解析 inventory/services.yaml 的任意顶层字段)
#   service_field <服务名> <字段名>   ->  回显字段值; 无该字段返回 1
# 例: service_field wireguard optional  ->  true
# ------------------------------------------------------------
_parse_services_fields() {
    # 解析结果按「整个文件」缓存: 本函数会被 service_field 反复调用 (面板状态表
    # 一次要查 ~40 组服务×字段), 每次都 fork 一个 awk 全量扫 YAML, 在 Git Bash
    # on Windows 上一轮实测 3 分钟。文件在单次进程生命周期内不会变, 可安全缓存。
    if [ -n "${_SVC_FIELDS_CACHE:-}" ]; then
        printf '%s\n' "$_SVC_FIELDS_CACHE"
        return 0
    fi
    local file="${LIB_NODES_PROJECT_DIR}/inventory/services.yaml"
    [ -f "$file" ] || return 0
    _SVC_FIELDS_CACHE="$(awk '
    BEGIN { cur = "" }
    {
        line = $0
        sub(/\r$/, "", line)
        if (line ~ /^[[:space:]]*#/ || line ~ /^[[:space:]]*$/) next
        if (line ~ /^[A-Za-z_]/) { cur = ""; next }
        # 服务名: 2 空格缩进的 "xxx:"
        if (line ~ /^  [A-Za-z_][A-Za-z0-9_-]*:/) {
            sub(/^  /, "", line); sub(/:.*$/, "", line)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", line)
            cur = line
            next
        }
        # 服务的属性字段: 4 空格缩进 "key: value" (排除列表项与子块)
        if (cur != "" && line ~ /^    [A-Za-z_][A-Za-z0-9_-]*:/) {
            sub(/^    /, "", line)
            p = index(line, ":")
            k = substr(line, 1, p - 1)
            v = substr(line, p + 1)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", k)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", v)
            gsub(/^"|"$/, "", v)
            # 只输出标量值, 跳过空值(子块起始) 与列表起始
            if (v != "") print cur "|" k "|" v
            next
        }
    }' "$file")"
    printf '%s\n' "$_SVC_FIELDS_CACHE"
}

# ------------------------------------------------------------
# services.yaml 字段索引 —— 预载进关联数组, 让查字段变成纯内存哈希访问
#
# 为什么需要它: 每个 (节点,服务) 的渲染要查 optional / install / port 三次字段,
# 18 组就是 54 次 service_field。而 service_field 即使命中缓存, 调用本身仍是
# `local x="$(service_field ...)"` 形式的子 shell —— 在 Git Bash on Windows 上
# 一次子 shell 约 1 秒, 54 次就是近一分钟。预载后查表退化为 ${_SVC_FIELD_INDEX[k]:-}。
#
# 键格式: <服务名>|<字段名>; 只填充**存在**的字段, 用 ${var+x} 区分"不存在"。
# ------------------------------------------------------------
declare -A _SVC_FIELD_INDEX=()
_SVC_FIELD_INDEX_LOADED=""

_services_field_index_load() {
    [ -n "$_SVC_FIELD_INDEX_LOADED" ] && return 0
    _SVC_FIELD_INDEX_LOADED=1
    local line name key value
    while IFS='|' read -r name key value; do
        [ -n "$name" ] && [ -n "$key" ] && _SVC_FIELD_INDEX["${name}|${key}"]="$value"
    done < <(_parse_services_fields)
}

service_field() {   # service_field <服务名> <字段名>
    local svc="$1" want="$2" v
    # 纯内存查表 (见上): 先把整份字段索引载入关联数组, 再直接取值。
    _services_field_index_load
    v="${_SVC_FIELD_INDEX["${svc}|${want}"]+_set}"
    if [ -z "$v" ]; then
        return 1
    fi
    printf '%s' "${_SVC_FIELD_INDEX["${svc}|${want}"]}"
}

# ------------------------------------------------------------
# 网络参数 mode 的裸值 (供 lib-services.sh 判定, 不在这里解释 auto)
# ------------------------------------------------------------
network_mode_raw() { printf '%s' "${NET_MODE:-auto}"; }

# ------------------------------------------------------------
# 本机数据根 (与远端 node_data_root 相对)
#   优先已 resolve 的 DATA_ROOT; 未 resolve 时返回空
#   远端读取请用 node_data_root <IP>
# ------------------------------------------------------------
local_data_root() {
    printf '%s' "${DATA_ROOT:-}"
}

# ------------------------------------------------------------
# 静态数据根 (不做任何 SSH) —— 生成类脚本专用
#
# 背景: node_data_root 会 SSH 到节点读 /etc/onecloud/install.conf, 单次
#       ConnectTimeout=4s, 节点离线时等于「每个节点白等 4~5 秒」。
#       gen-panel-config.sh 要在控制端为每个节点各取一次数据根,
#       3 节点就多花 ~15s, 且离线场景更久 —— 这是纯粹的构建期浪费。
#
# 生成器只需要一个「可写入配置文件的默认值」, 真实数据根由面板在运行时
# 自己探测 (panel/app.py), 因此这里不阻塞:
#   ONECLOUD_REMOTE_DATA_ROOT > 已 resolve 的 DATA_ROOT > /mnt/sd
#
# 需要节点真实数据根时, 仍用 node_data_root (会 SSH)。
# ------------------------------------------------------------
static_data_root() {
    if [ -n "${ONECLOUD_REMOTE_DATA_ROOT:-}" ]; then
        printf '%s' "$ONECLOUD_REMOTE_DATA_ROOT"
        return 0
    fi
    local r
    r="$(local_data_root)"
    if [ -n "$r" ]; then
        printf '%s' "$r"
        return 0
    fi
    printf '/mnt/sd'
}

require_nodes() {
    if [ "${#NODE_NAMES[@]}" -eq 0 ]; then
        echo "[ERROR] 未找到任何节点定义, 请检查: $NODES_YAML" >&2
        exit 1
    fi
}

load_nodes
