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
    NET_DNS="${ONECLOUD_DNS:-${_NODE_FIELD[__net__.dns]:-}}"
    [ -z "$NET_DNS" ] && NET_DNS="1.1.1.1"

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
    local raw val
    raw="$(node_field "$1" services)"
    [ -z "$raw" ] && return 0
    # 去掉方括号, 按逗号切分
    val="${raw#[}"
    val="${val%]}"
    local IFS=','
    local s
    for s in $val; do
        s="$(echo "$s" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
        [ -n "$s" ] && echo "$s"
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
require_nodes() {
    if [ "${#NODE_NAMES[@]}" -eq 0 ]; then
        echo "[ERROR] 未找到任何节点定义, 请检查: $NODES_YAML" >&2
        exit 1
    fi
}

load_nodes
