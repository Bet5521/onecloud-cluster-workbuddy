#!/bin/bash
# ============================================================
# 防火墙建议清单生成器 (firewall-recommend.sh)
#
# 定位:
#   部署脚本**不改防火墙**。本脚本读节点清单与服务清单, 为每个节点生成一份
#   「该怎么配防火墙」的建议清单 —— 清单里的 DSL 行可以直接抄进
#   setup_firewall.sh 的菜单录入, 也可以单独抽出来批量使用。
#
#   最终由人来执行防火墙配置, 因此这里只输出建议, 不执行任何 iptables/ufw/
#   firewall-cmd/nft 命令 (连查询都不做, 纯静态生成)。
#
# 用法:
#   ./scripts/firewall-recommend.sh                 # 全部节点 -> docs/firewall/<节点>.txt
#   ./scripts/firewall-recommend.sh wk-edge-01      # 只处理一个节点
#   ./scripts/firewall-recommend.sh --stdout        # 只打印到终端, 不落盘
#   ./scripts/firewall-recommend.sh --out DIR       # 指定输出目录
#   ./scripts/firewall-recommend.sh --emit-dsl      # 只输出 DSL 行 (可直接管道使用)
#   ./scripts/firewall-recommend.sh --lan 10.0.0.0/24   # 覆盖内网网段
# ============================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# 共享日志/清单库 (lib-nodes.sh 自带 log_info/log_warn/log_error 与 load_nodes)
# shellcheck source=lib-nodes.sh
source "${SCRIPT_DIR}/lib-nodes.sh"
# 安装态 / 组网模式 单一真相库 (未启用 WireGuard 时不生成任何 WG 相关规则)
# shellcheck source=lib-services.sh
source "${SCRIPT_DIR}/lib-services.sh"
require_nodes

SERVICES_YAML="${PROJECT_DIR}/inventory/services.yaml"
PANEL_PORT_DEFAULT=9000

# ---- 参数 ----
OUT_DIR="${PROJECT_DIR}/docs/firewall"
TO_STDOUT=false
EMIT_DSL_ONLY=false
LAN_SUBNET_OVERRIDE=""
TARGET_NODE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --stdout)    TO_STDOUT=true; shift ;;
        --emit-dsl)  EMIT_DSL_ONLY=true; TO_STDOUT=true; shift ;;
        --out)       OUT_DIR="$2"; shift 2 ;;
        --lan)       LAN_SUBNET_OVERRIDE="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        -*) log_error "未知选项: $1"; exit 2 ;;
        *)  TARGET_NODE="$1"; shift ;;
    esac
done

LAN_SUBNET="${LAN_SUBNET_OVERRIDE:-${NET_LAN_SUBNET:-192.168.1.0/24}}"
WG_PORT="${NET_WG_PORT:-51820}"

# Hub 节点: 承担外网端点与流量转发
#   先按 services.yaml 的声明反查; 未启用 WireGuard 时 wg_hub_node 返回空,
#   下面的 WG_ON 闸门会让所有 WG 相关规则与章节整体消失。
HUB_NODE="$(wg_hub_node 2>/dev/null || node_name_by_role edge-gateway 2>/dev/null || echo "${NODE_NAMES[0]}")"

# 是否生成 WireGuard 相关规则/章节
#   组网模式为 lan 时不生成 —— 否则会给出"放行 UDP 51820"这类无意义建议,
#   用户照做会在防火墙上开一个没有任何服务监听的公网端口。
WG_ON="$(wg_enabled)"

# ------------------------------------------------------------
# host 网络模式的服务: 端口由应用自身决定, 清单里声明的 ports/port 用不上,
# 这里按项目实际部署给出映射 (与 README「服务端口矩阵」保持一致)
# 每行 "协议 端口"
# ------------------------------------------------------------
hostnet_ports() {
    case "$1" in
        adguard)       printf 'tcp 3000\ntcp 53\nudp 53\n' ;;
        homeassistant) printf 'tcp 8123\n' ;;
    esac
    return 0
}

# ------------------------------------------------------------
# 解析 services.yaml 里每个服务声明的宿主端口
#   输出: <服务名>|<节点>|<协议>|<宿主端口>|<来源>
#   来源: ports = ports 列表; port = 标量 port; host = host 网络模式
# ------------------------------------------------------------
parse_service_ports() {
    [ -f "$SERVICES_YAML" ] || return 0
    awk '
    function emit(   n, i, a, p, proto, hp, src) {
        if (svc == "") return
        if (netmode == "host") { print svc "|" node "|any|-|host"; return }
        if (plist != "") {
            n = split(plist, a, " ")
            for (i = 1; i <= n; i++) {
                p = a[i]
                proto = "tcp"
                if (p ~ /\/udp$/)      { proto = "udp"; sub(/\/udp$/, "", p) }
                else if (p ~ /\/tcp$/) { sub(/\/tcp$/, "", p) }
                hp = p
                sub(/:.*$/, "", hp)
                if (hp != "") print svc "|" node "|" proto "|" hp "|ports"
            }
            return
        }
        if (port != "") print svc "|" node "|tcp|" port "|port"
    }
    BEGIN { svc = ""; key = ""; node = ""; netmode = ""; plist = ""; port = "" }
    {
        line = $0
        sub(/\r$/, "", line)
        if (line ~ /^[[:space:]]*#/ || line ~ /^[[:space:]]*$/) next
        if (line ~ /^[A-Za-z_]/) { emit(); svc = ""; next }
        if (line ~ /^  [A-Za-z_][A-Za-z0-9_-]*:[[:space:]]*$/) {
            emit()
            svc = line; sub(/^  /, "", svc); sub(/:.*$/, "", svc)
            node = ""; netmode = ""; plist = ""; port = ""; key = ""
            next
        }
        if (svc == "") next
        if (line ~ /^    [A-Za-z_][A-Za-z0-9_-]*:/) {
            f = line; sub(/^    /, "", f); sub(/:.*$/, "", f); key = f
            v = line; sub(/^    [A-Za-z_][A-Za-z0-9_-]*:[[:space:]]*/, "", v)
            if (f == "node")         node = v
            if (f == "port")         port = v
            if (f == "network_mode") netmode = v
            next
        }
        if (line ~ /^      -[[:space:]]/) {
            if (key != "ports") next
            v = line; sub(/^      -[[:space:]]*/, "", v)
            gsub(/"/, "", v); gsub(/[[:space:]]+$/, "", v)
            if (v != "") plist = plist (plist == "" ? "" : " ") v
            next
        }
    }
    END { emit() }' "$SERVICES_YAML"
}

# 该服务是否经 Cloudflare Tunnel 对外暴露 (那样公网无需在防火墙上开端口)
service_is_tunneled() {
    local svc="$1"
    [ -f "$SERVICES_YAML" ] || return 1
    awk -v want="$svc" '
    BEGIN { svc = ""; pub = 0 }
    { line = $0; sub(/\r$/, "", line) }
    line ~ /^  [A-Za-z_][A-Za-z0-9_-]*:[[:space:]]*$/ {
        if (svc == want && pub == 1) { found = 1 }
        svc = line; sub(/^  /, "", svc); sub(/:.*$/, "", svc); pub = 0; next
    }
    line ~ /^    public_access:[[:space:]]*$/ && svc == want { pub = 1 }
    line ~ /^      type:[[:space:]]*cloudflare-tunnel/ && pub == 1 { found = 1 }
    END { exit(found ? 0 : 1) }' "$SERVICES_YAML"
}

# 端口去重 + 格式校验 (依赖调用方的局部关联数组 seen, 利用 bash 动态作用域)
#   _add_port <协议> <端口> <说明> <范围>
#   范围: pub = 任意来源; lan = 仅内网; tunnel = 公网走 CF Tunnel; var = 需人工确认
_add_port() {
    local _p="$1" _port="$2" _desc="$3" _scope="$4"
    [ -z "$_port" ] && return 0
    case "$_port" in
        *'$'*|*'{'*)
            echo "${_p}|${_port}|${_desc}|var"
            return 0 ;;
    esac
    case "$_port" in
        *[!0-9:,-]*)
            echo "${_p}|${_port}|${_desc}|var"
            return 0 ;;
    esac
    [ "${seen[${_p}/${_port}]:-}" = "1" ] && return 0
    seen[${_p}/${_port}]=1
    echo "${_p}|${_port}|${_desc}|${_scope}"
}

# 收集某节点的端口建议: 输出 "<协议>|<端口>|<说明>|<范围>"
collect_ports() {
    local node="$1" svc node_name proto port src who
    local -A seen=()

    # SSH 是管理入口, 任何后端都要先放行
    _add_port tcp 22 "SSH 管理入口" pub

    # 面板 (在 nodes.yaml 的 services 里声明为 panel 的节点)
    for svc in $(node_services "$node" 2>/dev/null); do
        if [ "$svc" = "panel" ]; then
            _add_port tcp "$PANEL_PORT_DEFAULT" "集群面板 (PANEL_PORT)" lan
        fi
    done

    # WireGuard 只在 Hub 上需要入站, 且仅在组网模式启用 WireGuard 时
    if [ "$WG_ON" = "1" ] && [ "$node" = "$HUB_NODE" ]; then
        _add_port udp "$WG_PORT" "WireGuard 端点 (外网客户端接入)" pub
    fi

    # services.yaml 里声明的端口
    while IFS='|' read -r svc node_name proto port src; do
        [ -z "${svc:-}" ] && continue
        [ "$node_name" = "$node" ] || continue

        # 未安装的服务不开端口
        #   services.yaml 是"声明"而不是"实装": wireguard 在清单里始终列着
        #   (便于日后切模式), verysync 标了 install: manual。照单开端口会在
        #   防火墙上留下一个没有任何进程监听的公网 UDP 51820 —— 既无用又
        #   多一个攻击面。以 service_installed 为准, 与面板/健康检查同一真相。
        if [ "$(service_installed "$node" "$svc")" != "1" ]; then
            continue
        fi

        if service_is_tunneled "$svc"; then who="tunnel"; else who="lan"; fi

        if [ "$src" = "host" ]; then
            # host 网络: 端口由应用自身决定, 查映射表
            local hp_p hp_port
            while read -r hp_p hp_port; do
                [ -z "${hp_p:-}" ] && continue
                _add_port "$hp_p" "$hp_port" "${svc} (host 网络)" "$who"
            done <<< "$(hostnet_ports "$svc")"
            continue
        fi

        [ "$port" = "-" ] && continue
        _add_port "$proto" "$port" "$svc" "$who"
    done < <(parse_service_ports)
}

# ------------------------------------------------------------
# 生成单个节点的清单
# ------------------------------------------------------------
render_node() {
    local node="$1"
    local ip wg host role disp
    ip="$(node_ip "$node" 2>/dev/null)"
    wg="$(node_wg_ip "$node" 2>/dev/null)"
    host="$(node_hostname "$node" 2>/dev/null)"
    role="$(node_role "$node" 2>/dev/null)"
    disp="$(node_display_name "$node" 2>/dev/null)"
    [ -n "$disp" ] || disp="$node"

    local pub_lines="" lan_lines="" var_lines="" pub_notes="" lan_notes=""
    local proto port desc scope
    while IFS='|' read -r proto port desc scope; do
        [ -z "${proto:-}" ] && continue
        case "$scope" in
            pub)
                pub_lines="${pub_lines}in accept ${proto} ${port} - -"$'\n'
                pub_notes="${pub_notes}  * ${proto}/${port}  ${desc} —— 需从任意来源可达"$'\n'
                ;;
            lan)
                lan_lines="${lan_lines}in accept ${proto} ${port} ${LAN_SUBNET} -"$'\n'
                lan_notes="${lan_notes}  * ${proto}/${port}  ${desc} —— 仅内网 (已限定来源 ${LAN_SUBNET})"$'\n'
                ;;
            tunnel)
                lan_lines="${lan_lines}in accept ${proto} ${port} ${LAN_SUBNET} -"$'\n'
                lan_notes="${lan_notes}  * ${proto}/${port}  ${desc} —— 公网经 Cloudflare Tunnel, 无需开公网端口; 上面按\"仅内网\"给"$'\n'
                ;;
            var)
                var_lines="${var_lines}  ?  ${proto}/${port}  ${desc}"$'\n'
                ;;
        esac
    done < <(collect_ports "$node")
    local note_lines="${pub_notes}${lan_notes}"

    if [ "$EMIT_DSL_ONLY" = true ]; then
        printf '%s' "$pub_lines"
        printf '%s' "$lan_lines"
        return 0
    fi

    # 未启用 WireGuard 时不显示 wg 地址与 WG 相关描述, 避免留下误导线索
    local wg_line=""
    [ "$WG_ON" = "1" ] && wg_line="    WireGuard: ${wg}"

    cat << EOF
================================================================
 OneCloud 防火墙建议清单 — ${node} (${disp})
 生成时间: $(date '+%Y-%m-%d %H:%M:%S')
 节点地址: ${ip}${wg_line}    主机名: ${host}
 角色    : ${role}
 组网模式: $(network_mode_label)
================================================================

【0】先读这段
  这是**建议**清单, 不是配置。onecloud 的部署脚本不会改防火墙:
  改防火墙的唯一入口是你手动执行的 setup_firewall.sh。
  确认下面的内容符合预期后, 再逐条录入并应用。

【1】建议的默认策略  (setup_firewall.sh 主菜单 4)
  INPUT   = DROP      入站白名单: 没列出来的都不放行
  OUTPUT  = ACCEPT    出站放行; 只有在你有明确出站管控需求时才改成 DROP
  FORWARD = ACCEPT    **保持 ACCEPT** —— Docker / 容器网络 / 网桥依赖它,
                      改成 DROP 会让容器直接断网

【2】建议放行的入站规则  (主菜单 2 → 1/2/3 录入)

  ── 必需 (任意来源) ──
${pub_lines}
  ── 仅内网 (来源限定 ${LAN_SUBNET}) ──
${lan_lines}
  逐条说明:
${note_lines}
  说明: 内网服务不限定来源也能用, 但一旦这台机器有别的网段可达 (WireGuard
        客户端、访客网络), 未限来源就等于对所有网段开放。限定来源更安全。

EOF

    if [ -n "$var_lines" ]; then
        cat << EOF
  ── 需要你人工确认 (端口不是字面值, 无法直接写成规则) ──
${var_lines}
  这些端口在 services.yaml 里写的是变量 (如 \${MEMOS_PORT}), 实际取值在
  节点 .env 里。确认后用  in accept tcp <真实端口> ${LAN_SUBNET} -  录入。

EOF
    fi

    cat << EOF
【3】SSH 防暴力破解  (主菜单 5)
  建议: 启用 / 端口 22 / 时间窗 60 秒 / 触发 5 次
  各后端的实现: iptables 用 recent, nftables 用 meter, firewalld 用 rich
  rule limit, ufw 用 limit —— 语义一致, 换后端不用改思路。

EOF

    if [ "$WG_ON" != "1" ]; then
        cat << EOF
【4】本节点不需要 WireGuard 相关规则
  当前组网模式为 $(network_mode), 未启用 WireGuard。
  因此本清单**不含** UDP ${WG_PORT} 放行, 也不需要转发 / NAT 规则 (FORWARD、
  POSTROUTING MASQUERADE) —— 照上面【2】的端口清单配即可。
  (通信走局域网直连, 各节点 IP 见上文"节点地址"。)

EOF
    elif [ "$node" = "$HUB_NODE" ]; then
        cat << EOF
【4】DSL 表达不了的部分 —— WireGuard 转发 (本节点是 Hub, 必做)
  转发 + NAT 不是"放行端口", DSL 里没有对应写法, 需要在节点上单独执行:

    WG_IF=\$(ip -4 route show default scope global | awk '{print \$5; exit}')
    iptables -C FORWARD -i wg0 -j ACCEPT || iptables -A FORWARD -i wg0 -j ACCEPT
    iptables -C FORWARD -o wg0 -j ACCEPT || iptables -A FORWARD -o wg0 -j ACCEPT
    iptables -t nat -C POSTROUTING -o "\$WG_IF" -j MASQUERADE || \\
        iptables -t nat -A POSTROUTING -o "\$WG_IF" -j MASQUERADE

  三条注意:
    1. \$WG_IF 必须在节点上探测 —— 玩客云刷 Armbian 后网卡常是 end0 而非
       eth0, 写死会让 MASQUERADE 静默失效 (wg 握手正常, 客户端上不了网)。
    2. 用 -C 探测后再 -A, 反复 up/down 才不会把规则堆成一摞。
    3. 本机的 wg0.conf 默认**不**自带这些规则 (见 wg0.conf 里的注释),
       就是为了避免和这份清单里的规则互相覆盖。

  另外确认内核转发已开:
    sysctl -w net.ipv4.ip_forward=1
    # 持久化: 写入 /etc/sysctl.d/99-onecloud.conf

EOF
    else
        cat << EOF
【4】本节点不需要 WireGuard 转发
  只有 Hub 节点 (${HUB_NODE}) 需要 FORWARD / NAT 规则。本节点只需要放行
  上面的 UDP ${WG_PORT} 之外的常规端口 (若有需要)。

EOF
    fi

    cat << EOF
【5】应用与验证步骤
  1. 先确认你当前 SSH 占用的端口就是上面放行的那个:
       ss -tlnp | grep sshd      # 或 grep -i '^Port' /etc/ssh/sshd_config
     setup_firewall.sh 会自动读 SSH_CONNECTION / sshd_config, 通常在 22。
  2. 在节点上执行:  sudo bash setup_firewall.sh
  3. 按【2】录入规则, 按【1】设默认策略, 按【3】开 SSH 防暴破。
  4. 主菜单 7) 应用规则到运行时(试运行, 需确认)
     —— 应用后会给你 20 秒确认窗口: **另开一个 SSH 窗口**验证能登录,
        确认没问题再在原窗口按 y; 不按或超时会自动回滚。
  5. 主菜单 8) 保存并持久化(开机自启)
  6. 主菜单 1) 查看当前状态 复核。

【6】回滚
  * setup_firewall.sh 主菜单 9) 备份与回滚
  * 主菜单 10) 重置为全放行
  * 应急(本地终端): iptables -P INPUT ACCEPT; iptables -F

【7】本次未做的事
  * 没有检查节点上的现有规则 —— 本清单是静态生成的, 不含任何实际查询。
    应用前请先用主菜单 1) 看一眼现状, 避免覆盖掉别的工具写的规则。
  * 没有开任何端口。防火墙仍然是"没动过"的状态。
EOF
}

# ------------------------------------------------------------
# 入口
# ------------------------------------------------------------
NODES_TO_DO=()
if [ -n "$TARGET_NODE" ]; then
    if ! node_resolve "$TARGET_NODE" >/dev/null 2>&1; then
        log_error "未找到节点: ${TARGET_NODE}"
        log_info  "可用节点: $(node_names | tr '\n' ' ')"
        exit 1
    fi
    NODES_TO_DO=("$TARGET_NODE")
else
    while IFS= read -r _n; do
        [ -n "$_n" ] && NODES_TO_DO+=("$_n")
    done < <(node_names)
fi

if [ "$EMIT_DSL_ONLY" = true ]; then
    for n in "${NODES_TO_DO[@]}"; do
        render_node "$n"
    done
    exit 0
fi

if [ "$TO_STDOUT" = true ]; then
    for n in "${NODES_TO_DO[@]}"; do
        render_node "$n"
        echo ""
    done
    exit 0
fi

mkdir -p "$OUT_DIR" || { log_error "无法创建输出目录: ${OUT_DIR}"; exit 1; }

log_info "生成防火墙建议清单 (内网网段 ${LAN_SUBNET})"
for n in "${NODES_TO_DO[@]}"; do
    f="${OUT_DIR}/${n}.txt"
    render_node "$n" > "$f" || { log_error "生成失败: ${n}"; continue; }
    log_info "  ${f#${PROJECT_DIR}/}  ($(grep -c '^in accept' "$f" || true) 条建议规则)"
done

echo ""
log_info "全部完成。清单只是建议 —— 防火墙没有被本脚本改动过。"
log_warn "下一步: 把清单拷到对应节点, 由你执行 setup_firewall.sh 逐条录入并应用"
