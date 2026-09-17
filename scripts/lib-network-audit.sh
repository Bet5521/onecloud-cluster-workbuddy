#!/bin/bash
# ============================================================
# OneCloud · 网络通路 / 防火墙 / SSH 通道自检库
#   scripts/lib-network-audit.sh
#
# 提供 (全部只读, 不修改任何系统状态):
#   net_audit_is_ssh            当前是否运行在 SSH 会话中 (0/1)
#   net_audit_ssh_client        输出 "客户端IP 客户端端口 服务端IP 服务端端口"
#   net_audit_ssh_ports         输出 sshd 监听端口 (每行一个, 默认 22)
#   net_audit_backend           输出防火墙后端: iptables|nftables|ufw|firewalld|none|unknown
#   net_audit_policy CHAIN      输出链默认策略: ACCEPT|DROP|REJECT|unknown
#   net_audit_ssh_allowed       0=SSH 端口被放行(或策略宽松) 1=疑似被拦
#   net_audit_docker_present    0=Docker 已安装
#   net_audit_wg_hub            0=本机是 WireGuard 出口节点 (edge-gateway)
#   net_audit_egress_if         输出默认路由出口网卡 (取不到返回 1)
#   net_audit_report            打印人类可读报告; 命中风险返回 1
#
# 背景:
#   本仓库的脚本不会主动改防火墙 (唯一动 iptables 的地方是 wg-quick 的
#   PostUp/PostDown), 但现场真正"把人挡在门外"的往往是别的东西:
#     * INPUT 策略已被改成 DROP 却没有放行 SSH 端口
#     * ufw / firewalld 被启用而没放行 SSH
#     * Docker 装完后把 FORWARD 策略改成 DROP, 顺手打断 WireGuard 转发
#     * 通过 SSH 远端改静态 IP —— 配置一 apply 当前会话就断
#   本库把这些状态查出来报告给操作者, 而不是替他去改。
#
# 约定 (与 lib-nodes.sh / lib-pydeps.sh / lib-panel-host.sh 一致):
#   - 本库不得 set -e / set -u; 由调用方决定失败处理
#   - 本库不定义 log_* (各调用方命名不同), 只把结果写到 stdout 与结果变量
#   - 所有探测在命令缺失 / 无 root 权限时都必须安全降级为 unknown, 不得报错中断
# ============================================================

# ------------------------------------------------------------
# 当前是否 SSH 会话 (bootstrap 改 IP 前的失联判断依赖它)
#   SSH_CONNECTION="<客户端IP> <客户端端口> <服务端IP> <服务端端口>"
#   SSH_CLIENT 是旧变量, 部分精简环境只给这一个
# ------------------------------------------------------------
net_audit_is_ssh() {
    [ -n "${SSH_CONNECTION:-}" ] || [ -n "${SSH_CLIENT:-}" ]
}

# 输出 "客户端IP 客户端端口 服务端IP 服务端端口" (取不到则空)
net_audit_ssh_client() {
    if [ -n "${SSH_CONNECTION:-}" ]; then
        printf '%s\n' "$SSH_CONNECTION"
        return 0
    fi
    if [ -n "${SSH_CLIENT:-}" ]; then
        # SSH_CLIENT 只有 "客户端IP 客户端端口 服务端端口"
        # shellcheck disable=SC2086
        set -- $SSH_CLIENT
        printf '%s %s %s %s\n' "${1:-}" "${2:-}" "" "${3:-}"
        return 0
    fi
    printf '\n'
    return 1
}

# ------------------------------------------------------------
# sshd 实际监听端口
#   优先取运行中进程的监听 (ss/lsof), 退化到 sshd_config, 最后默认 22
#   ONECLOUD_ETC_ROOT 生效 (测试时把 /etc 指到别处)
# ------------------------------------------------------------
net_audit_ssh_ports() {
    local ports="" cfg="${ONECLOUD_ETC_ROOT:-/etc}/ssh/sshd_config" p
    if command -v ss >/dev/null 2>&1; then
        ports="$(ss -tlnH 2>/dev/null | awk '$4 ~ /:22$/ || $0 ~ /sshd/ { n = split($4, a, ":"); print a[n] }' | sort -u)"
    fi
    if [ -z "$ports" ] && command -v lsof >/dev/null 2>&1; then
        ports="$(lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | awk '/sshd/ { n = split($9, a, ":"); print a[n] }' | sort -u)"
    fi
    if [ -z "$ports" ] && [ -r "$cfg" ]; then
        ports="$(grep -iE '^[[:space:]]*Port[[:space:]]+[0-9]+' "$cfg" 2>/dev/null \
                 | awk '{print $2}' | sort -u)"
    fi
    [ -n "$ports" ] || ports="22"
    for p in $ports; do
        printf '%s\n' "$p"
    done
    return 0
}

# ------------------------------------------------------------
# 防火墙后端判定 (按"是否真的在生效"排序: ufw/firewalld 是管理前端, 优先识别)
# ------------------------------------------------------------
net_audit_backend() {
    if command -v ufw >/dev/null 2>&1; then
        if ufw status 2>/dev/null | grep -qi '^Status:[[:space:]]*active'; then
            printf 'ufw\n'; return 0
        fi
    fi
    if command -v firewall-cmd >/dev/null 2>&1; then
        if firewall-cmd --state 2>/dev/null | grep -qi running; then
            printf 'firewalld\n'; return 0
        fi
    fi
    if command -v nft >/dev/null 2>&1; then
        if nft list ruleset 2>/dev/null | grep -q .; then
            printf 'nftables\n'; return 0
        fi
    fi
    if command -v iptables >/dev/null 2>&1; then
        # 只有默认策略或自定义规则存在时才算"有防火墙在管事"
        if iptables -S 2>/dev/null | grep -qvE '^(-P (INPUT|FORWARD|OUTPUT) ACCEPT|$)'; then
            printf 'iptables\n'; return 0
        fi
        printf 'none\n'; return 0
    fi
    printf 'unknown\n'
}

# ------------------------------------------------------------
# 链默认策略 (ACCEPT / DROP / REJECT / unknown)
#   无权限读取 (非 root) 时返回 unknown, 不当作风险
# ------------------------------------------------------------
net_audit_policy() {
    local chain="${1:-INPUT}" p=""
    if command -v iptables >/dev/null 2>&1; then
        p="$(iptables -S "$chain" 2>/dev/null | awk '/^-P /{print $3; exit}')"
    fi
    if [ -z "$p" ] && command -v nft >/dev/null 2>&1; then
        p="$(nft list chain inet filter "$chain" 2>/dev/null \
             | awk -F'policy ' '/policy/ {split($2, a, ";"); print a[1]; exit}')"
    fi
    case "$p" in
        ACCEPT|DROP|REJECT) printf '%s\n' "$p" ;;
        *) printf 'unknown\n' ;;
    esac
}

# ------------------------------------------------------------
# SSH 端口是否被放行
#   0 = 放行 (或本就没有拦的规则)
#   1 = 疑似被拦 —— 此时改网络配置/重启后很可能再也连不上
# ------------------------------------------------------------
net_audit_ssh_allowed() {
    local backend ports port
    backend="$(net_audit_backend)"
    ports="$(net_audit_ssh_ports | tr '\n' ' ')"

    case "$backend" in
        ufw)
            for port in $ports; do
                ufw status 2>/dev/null | grep -E "^${port}(/tcp)?[[:space:]]" | grep -qi ALLOW && return 0
            done
            # 未显式放行, 但策略默认放行 (ufw default allow incoming)
            ufw status verbose 2>/dev/null | grep -qi 'Default: allow (incoming)' && return 0
            return 1
            ;;
        firewalld)
            for port in $ports; do
                firewall-cmd --list-ports 2>/dev/null | tr ' ' '\n' | grep -qx "${port}/tcp" && return 0
            done
            firewall-cmd --list-services 2>/dev/null | tr ' ' '\n' | grep -qx 'ssh' && return 0
            return 1
            ;;
        iptables|nftables)
            local pol
            pol="$(net_audit_policy INPUT)"
            [ "$pol" = "unknown" ] && return 0        # 读不到就别吓人
            [ "$pol" = "ACCEPT" ] && return 0
            if command -v iptables >/dev/null 2>&1; then
                for port in $ports; do
                    iptables -S INPUT 2>/dev/null \
                        | grep -E -- "--dport ${port}([[:space:]]|$)" \
                        | grep -qE 'ACCEPT' && return 0
                done
            fi
            return 1
            ;;
        *)
            # none: 没有防火墙; unknown: 判断不了 —— 都不算风险
            return 0
            ;;
    esac
}

# ------------------------------------------------------------
# Docker 是否已安装 (它会在启动时把 FORWARD 策略改成 DROP, 并插入自己的链)
# ------------------------------------------------------------
net_audit_docker_present() {
    if command -v docker >/dev/null 2>&1; then
        return 0
    fi
    if [ -d /sys/class/net/docker0 ] || [ -d /var/lib/docker ]; then
        return 0
    fi
    command -v dpkg >/dev/null 2>&1 && dpkg -l 2>/dev/null | grep -q '^ii[[:space:]]\+docker' && return 0
    return 1
}

# ------------------------------------------------------------
# 本机是否为 WireGuard 出口节点 (清单里 role=edge-gateway 的那台的 LAN IP)
#   判断不出时返回 1 (宁可漏报, 不要误报)
# ------------------------------------------------------------
net_audit_wg_hub() {
    local lib="${BASH_SOURCE[0]%/*}/lib-nodes.sh" hub ip
    [ -f "$lib" ] || return 1
    # 已在别处 source 过就不重复加载
    if ! declare -F node_name_by_role >/dev/null 2>&1; then
        # shellcheck source=lib-nodes.sh
        . "$lib" 2>/dev/null || return 1
    fi
    hub="$(node_name_by_role edge-gateway 2>/dev/null || true)"
    [ -n "$hub" ] || return 1
    ip="$(node_ip "$hub" 2>/dev/null || true)"
    [ -n "$ip" ] || return 1
    net_audit_local_has_ip "$ip"
}

# 本机是否持有某地址 (自检用; 不依赖 lib-panel-host.sh 的探测顺序)
net_audit_local_has_ip() {
    local want="${1:-}" a
    [ -n "$want" ] || return 1
    if command -v ip >/dev/null 2>&1; then
        ip -4 -o addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1 \
            | grep -qx "$want" && return 0
    fi
    for a in $(hostname -I 2>/dev/null); do
        [ "$a" = "$want" ] && return 0
    done
    return 1
}

# ------------------------------------------------------------
# 默认路由出口网卡 (WireGuard MASQUERADE 要用真实网卡名, 不能写死 eth0)
# ------------------------------------------------------------
net_audit_egress_if() {
    local i=""
    if command -v ip >/dev/null 2>&1; then
        i="$(ip -4 route show default scope global 2>/dev/null | awk '{print $5; exit}')"
        [ -n "$i" ] || i="$(ip -4 route show default 2>/dev/null | awk '{print $5; exit}')"
    fi
    if [ -z "$i" ] && command -v route >/dev/null 2>&1; then
        i="$(route -n 2>/dev/null | awk '$1 == "0.0.0.0" {print $8; exit}')"
    fi
    [ -n "$i" ] || return 1
    printf '%s\n' "$i"
}

# ------------------------------------------------------------
# 汇总报告
#   只打印, 不改系统; 命中风险时返回 1 (调用方决定是告警还是中断)
#   风险等级:
#     [高危] 现状就会挡 SSH —— 先解决再动网络配置
#     [注意] 不影响 SSH, 但会影响 WireGuard/容器转发
# ------------------------------------------------------------
net_audit_report() {
    local backend in_pol fw_pol ssh_ports ssh_bad=false risk=0
    backend="$(net_audit_backend)"
    in_pol="$(net_audit_policy INPUT)"
    fw_pol="$(net_audit_policy FORWARD)"
    ssh_ports="$(net_audit_ssh_ports | tr '\n' ' ')"
    ssh_ports="${ssh_ports% }"

    echo "  ── 通路自检 (只读, 不修改任何设置) ──"

    if net_audit_is_ssh; then
        # shellcheck disable=SC2046
        set -- $(net_audit_ssh_client)
        echo "    会话      : SSH 远程 (来自 ${1:-未知})"
    else
        echo "    会话      : 本地控制台"
    fi

    echo "    sshd 端口 : ${ssh_ports:-未检测到}"

    case "$backend" in
        none)    echo "    防火墙    : 未启用 (无自定义规则, INPUT 默认 ${in_pol})" ;;
        unknown) echo "    防火墙    : 未能探测 (缺少权限或命令; 需要 root 才能读取规则)" ;;
        *)       echo "    防火墙    : ${backend} 生效中 (INPUT 默认策略 ${in_pol})" ;;
    esac

    if ! net_audit_ssh_allowed; then
        ssh_bad=true
        risk=1
        echo "    [高危]    : 当前规则未放行 SSH 端口 (${ssh_ports}), 改网络配置或重启后可能直接失联"
        echo "              先放行再继续, 例如: iptables -I INPUT -p tcp --dport ${ssh_ports%% *} -j ACCEPT"
    fi

    # FORWARD DROP 对"出口网关"是实打实的功能故障, 对普通节点无所谓
    if [ "$fw_pol" = "DROP" ] || [ "$fw_pol" = "REJECT" ]; then
        if net_audit_wg_hub; then
            risk=1
            echo "    [注意]    : FORWARD 默认策略为 ${fw_pol}, 而本机是 WireGuard 出口节点"
            if net_audit_docker_present; then
                echo "              Docker 启动时会把 FORWARD 置 DROP, 需在 DOCKER-USER 链放行 wg0:"
                echo "              iptables -I DOCKER-USER -i wg0 -j ACCEPT"
            else
                echo "              WireGuard 客户端流量将被丢弃, 需放行 wg0 转发"
            fi
        else
            echo "    [注意]    : FORWARD 默认策略为 ${fw_pol} (容器/转发类服务可能受影响)"
        fi
    fi

    if [ "$risk" -eq 0 ] && [ "$ssh_bad" = false ]; then
        echo "    结论      : 未发现会切断 SSH 通道的隐患"
    fi
    [ "$risk" -eq 0 ]
}
