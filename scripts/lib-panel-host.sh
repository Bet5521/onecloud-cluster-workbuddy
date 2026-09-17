#!/bin/bash
# ============================================================
# OneCloud · 面板监听地址工具库
#   scripts/lib-panel-host.sh
#
# 提供:
#   panel_detect_local_ipv4   列出本机可用于监听的 IPv4 (每行一个, 已去重)
#   panel_host_check          校验监听地址, 识别"网络地址/广播/回环网段"等误填
#   panel_host_is_local       地址是否在本机某张网卡上
#   panel_host_desc           人类可读的访问范围描述
#   panel_host_cidr           本机地址所属网段 (a.b.c.d/len)
#
# 背景:
#   面板监听地址此前是自由文本、零校验。把网段地址 (如 192.168.1.0) 或
#   回环网段的网络地址 (127.0.0.0) 填进去不会当场报错, 但要等到 systemd
#   启动 app.py 才以 "Cannot assign requested address" 失败 —— 排查成本高。
#   本库把这些取值在校验阶段就挡下来, 并尽量给出可直接采用的替代值。
#
# 约定 (与 lib-nodes.sh / lib-pydeps.sh 一致):
#   - 本库不得 set -e / set -u; 由调用方决定失败处理
#   - 本库不定义 log_* (各调用方命名不同), 只把结果写到 stdout 与结果变量
#   - 校验结果统一走 PANEL_HOST_REASON / PANEL_HOST_SUGGEST 两个全局变量
# ============================================================

# ------------------------------------------------------------
# 列出本机可对外监听的 IPv4 (排除回环; 每行一个, 保持出现顺序去重)
# 三级兜底: iproute2 -> hostname -I -> ifconfig
# ------------------------------------------------------------
panel_detect_local_ipv4() {
    local a
    if command -v ip >/dev/null 2>&1; then
        ip -4 -o addr show scope global 2>/dev/null \
            | awk '{print $4}' | cut -d/ -f1 | awk '!seen[$0]++'
        return 0
    fi
    if command -v hostname >/dev/null 2>&1; then
        for a in $(hostname -I 2>/dev/null); do
            case "$a" in
                ''|*:*|127.*) continue ;;
            esac
            printf '%s\n' "$a"
        done | awk '!seen[$0]++'
        return 0
    fi
    if command -v ifconfig >/dev/null 2>&1; then
        ifconfig 2>/dev/null | awk '/inet /{print $2}' | sed 's/^addr://' \
            | grep -v '^127\.' | awk '!seen[$0]++'
    fi
    return 0
}

# ------------------------------------------------------------
# 由网段地址 (a.b.c.0 / a.b.c.255) 推断本机在该网段的地址
# 用于把「想要同网段访问」的模糊意图落到一个真实可绑的地址上
# 拿不到同网段地址则返回 1
# ------------------------------------------------------------
panel_host_lan_peer() {
    local ip="${1:-}" head3 a
    head3="${ip%.*}"
    while IFS= read -r a; do
        case "$a" in
            "${head3}."*) printf '%s\n' "$a"; return 0 ;;
        esac
    done < <(panel_detect_local_ipv4)
    return 1
}

# ------------------------------------------------------------
# 判断地址是否属于本机 (127.0.0.1 单独放行: 任何机器都有回环)
# ------------------------------------------------------------
panel_host_is_local() {
    local ip="${1:-}" a
    [ "$ip" = "127.0.0.1" ] && return 0
    while IFS= read -r a; do
        [ "$a" = "$ip" ] && return 0
    done < <(panel_detect_local_ipv4)
    return 1
}

# ------------------------------------------------------------
# 本机地址所属网段 (网络地址 + 前缀, 如 192.168.1.0/24)
# 前缀取自网卡实际配置, 拿不到时按 /24 兜底; 仅用于展示
# ------------------------------------------------------------
panel_host_cidr() {
    local ip="${1:-}" cidr len
    if command -v ip >/dev/null 2>&1; then
        cidr="$(ip -4 -o addr show scope global 2>/dev/null \
                | awk -v ip="$ip" '{ split($4, a, "/"); if (a[1] == ip) { print $4; exit } }')"
    fi
    len="${cidr##*/}"
    case "$len" in
        ''|*[!0-9]*) len=24 ;;
    esac
    # 主机位清零, 得到网络地址 (与 bootstrap.sh 的 ip_net_info 同一算法)
    awk -v ip="$ip" -v p="$len" 'BEGIN {
        if (ip !~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/) exit 1
        if (split(ip, o, ".") != 4) exit 1
        if (p + 0 < 1 || p + 0 > 32) p = 24
        v = o[1]*16777216 + o[2]*65536 + o[3]*256 + o[4]
        size = 2 ^ (32 - p)
        if (size < 1) size = 1
        n = int(v / size) * size
        printf "%d.%d.%d.%d/%d\n", \
            int(n/16777216)%256, int(n/65536)%256, int(n/256)%256, n%256, p
    }'
}

# ------------------------------------------------------------
# 校验监听地址
#   合法 -> 返回 0
#   非法 -> 返回 1, 原因写入 PANEL_HOST_REASON, 可选替代值写入 PANEL_HOST_SUGGEST
#
# 下面这些"看起来像地址、实际绑不上"的取值都会失败:
#   127.0.0.0       回环网段的网络地址 (最典型的误填)
#   192.168.1.0     网段地址 —— 面板只能绑到某台主机的地址
#   192.168.1.255   广播地址
#   224.0.0.1       组播段
#   169.254.x.x     链路本地 (APIPA)
#   240.0.0.0/4     保留段
#   0.1.2.3         0/8 保留 (只有 0.0.0.0 合法, 表示全部网卡)
# ------------------------------------------------------------
panel_host_check() {
    local ip="${1:-}" o1 o2 o3 o4
    PANEL_HOST_REASON=""
    PANEL_HOST_SUGGEST=""

    if [ -z "$ip" ]; then
        PANEL_HOST_REASON="监听地址不能为空"
        return 1
    fi

    # 只接受 IPv4 字面量: app.py 直接把它交给 Flask 绑定, 不做名字解析
    if ! printf '%s\n' "$ip" | awk -F. '
            NF != 4 { exit 1 }
            {
                for (i = 1; i <= 4; i++) {
                    if ($i !~ /^[0-9]+$/) exit 1
                    if ($i + 0 > 255) exit 1
                }
            }'; then
        PANEL_HOST_REASON="不是合法的 IPv4 地址 (需形如 192.168.1.101; 不支持主机名与 IPv6)"
        PANEL_HOST_SUGGEST="$(panel_detect_local_ipv4 | head -n 1)"
        return 1
    fi

    IFS=. read -r o1 o2 o3 o4 <<< "$ip"

    # 0.0.0.0 = 全部网卡, 是 0/8 里唯一可用的取值
    if [ "$o1" -eq 0 ]; then
        if [ "$o2" -eq 0 ] && [ "$o3" -eq 0 ] && [ "$o4" -eq 0 ]; then
            return 0
        fi
        PANEL_HOST_REASON="0.0.0.0/8 是保留网段 (要监听全部网卡请写 0.0.0.0)"
        PANEL_HOST_SUGGEST="0.0.0.0"
        return 1
    fi

    # 127.0.0.0/8 回环: 只有 127.0.0.1 是常规写法
    if [ "$o1" -eq 127 ]; then
        if [ "$o2" -eq 0 ] && [ "$o3" -eq 0 ] && [ "$o4" -eq 1 ]; then
            return 0
        fi
        if [ "$o4" -eq 0 ]; then
            PANEL_HOST_REASON="${ip} 是回环网段的网络地址, 不是可用监听地址"
        else
            PANEL_HOST_REASON="${ip} 属 127.0.0.0/8 回环网段, 只有本机能访问"
        fi
        PANEL_HOST_SUGGEST="127.0.0.1"
        return 1
    fi

    # 组播 / 保留 / 链路本地: 都不可能作为单播监听地址
    if [ "$o1" -eq 255 ]; then
        PANEL_HOST_REASON="${ip} 是广播地址, 不是可用监听地址"
        PANEL_HOST_SUGGEST="$(panel_detect_local_ipv4 | head -n 1)"
        return 1
    fi
    if [ "$o1" -ge 240 ]; then
        PANEL_HOST_REASON="${ip} 属 240.0.0.0/4 保留段, 不能作为监听地址"
        PANEL_HOST_SUGGEST="$(panel_detect_local_ipv4 | head -n 1)"
        return 1
    fi
    if [ "$o1" -ge 224 ]; then
        PANEL_HOST_REASON="${ip} 属 224.0.0.0/4 组播段, 不能作为监听地址"
        PANEL_HOST_SUGGEST="$(panel_detect_local_ipv4 | head -n 1)"
        return 1
    fi
    if [ "$o1" -eq 169 ] && [ "$o2" -eq 254 ]; then
        PANEL_HOST_REASON="${ip} 属 169.254.0.0/16 链路本地段 (APIPA), 不适合长期监听"
        PANEL_HOST_SUGGEST="$(panel_detect_local_ipv4 | head -n 1)"
        return 1
    fi

    # 把「网段」当成地址填: 这是"想同网段可访问"最常见的表达方式, 要引到具体主机地址
    if [ "$o4" -eq 0 ]; then
        PANEL_HOST_REASON="${ip} 是网络地址 (整个网段), 面板只能绑定到某台主机的地址"
        PANEL_HOST_SUGGEST="$(panel_host_lan_peer "$ip")"
        return 1
    fi
    if [ "$o4" -eq 255 ]; then
        PANEL_HOST_REASON="${ip} 通常是网段的广播地址, 不是主机地址"
        PANEL_HOST_SUGGEST="$(panel_host_lan_peer "$ip")"
        return 1
    fi

    return 0
}

# ------------------------------------------------------------
# 人类可读的访问范围描述 (用于配置确认页与部署日志)
# ------------------------------------------------------------
panel_host_desc() {
    local ip="${1:-}"
    case "$ip" in
        0.0.0.0)   printf '%s\n' "全部网卡 (含 WireGuard / 外网网卡)"; return 0 ;;
        127.0.0.1) printf '%s\n' "仅本机 (远端访问需 SSH 端口转发)";      return 0 ;;
    esac
    if panel_host_is_local "$ip"; then
        printf '本机网卡地址 (同网段 %s 可访问)\n' "$(panel_host_cidr "$ip")"
    else
        printf '%s\n' "非本机网卡地址 (启动可能失败)"
    fi
}
