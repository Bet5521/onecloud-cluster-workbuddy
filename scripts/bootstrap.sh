#!/bin/bash
# ============================================================
# 玩客云节点初始化脚本 (bootstrap.sh)
# 在新刷好 Armbian 的玩客云上运行一次
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 本脚本常在尚未克隆仓库的新节点上运行, 因此清单可用则加载, 不可用则降级为
# 参数 / 环境变量 / 交互输入。IP 与主机名均不硬编码。
HAVE_INVENTORY=false
if [ -f "${SCRIPT_DIR}/lib-nodes.sh" ]; then
    # shellcheck source=lib-nodes.sh
    source "${SCRIPT_DIR}/lib-nodes.sh"
    if [ "${#NODE_NAMES[@]}" -gt 0 ]; then
        HAVE_INVENTORY=true
    fi
fi

# 网络通路 / 防火墙 / SSH 通道自检库 (纯只读探测)
# 库缺失时自检整段降级跳过, 不影响初始化主流程
HAVE_NET_AUDIT=false
if [ -f "${SCRIPT_DIR}/lib-network-audit.sh" ]; then
    # shellcheck source=lib-network-audit.sh
    source "${SCRIPT_DIR}/lib-network-audit.sh"
    if declare -F net_audit_report >/dev/null 2>&1; then
        HAVE_NET_AUDIT=true
    fi
fi

# 安装路径自适应库 (SD 卡状态探测 -> 动态选择 /opt 回退)
# 库缺失时降级为固定 /opt/onecloud, 不影响初始化主流程
if [ -f "${SCRIPT_DIR}/lib-install-path.sh" ]; then
    # shellcheck source=lib-install-path.sh
    source "${SCRIPT_DIR}/lib-install-path.sh"
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ------------------------------------------------------------
# 初始化装包清单 —— 面向无头服务器 (玩客云: 无图形界面 / 1GB 内存 / eMMC)
#
#   分档原则:
#     核心包   部署链路真的会调用, 缺了走不下去 -> 默认装
#     可选包   排障 / 编辑 / 老习惯兼容, 缺了只是不方便 -> 默认不装, 需显式开启
#              (--extra-pkgs 或 ONECLOUD_EXTRA_PKGS)
#     桌面/图形 一律不装: 目标机没有显示输出, 装了只吃空间与内存。
#              即便被显式列出, 也会被 pkg_gui_filter 剔除 (见 APT_GUI_DENY)
#
#   核心包逐项理由 (其余一律不进默认流程):
#     curl              下载 (docker 安装脚本 / GitHub release / get-pip.py)
#     git               克隆 onecloud-cluster 仓库 (初始化后第一步)
#     ca-certificates   HTTPS 校验证书; 缺了 curl 直接失败
#     jq                setup.sh 解析 JSON (GitHub release / 面板接口)
#     rsync             迁移 /var/lib/docker 到 SD 卡
#     parted            SD 卡分区 (mklabel / mkpart)
#     wireguard-tools   集群组网 (wg / wg-quick), 三节点互通的地基
#   iproute2 / e2fsprogs / util-linux 属系统基础包 (Priority: required/important),
#   系统一定自带, 不重复声明。
# ------------------------------------------------------------
BASE_PKGS="curl git ca-certificates jq rsync parted wireguard-tools"

# 可选包预设: 仅当显式开启 (--extra-pkgs) 时才安装
OPT_PKGS_PRESET="wget vim htop iotop net-tools dnsutils unzip dosfstools fdisk lsb-release gnupg"

# 桌面环境 / 图形组件黑名单 (无头服务器一律拒绝)
APT_GUI_DENY="task-desktop task-gnome-desktop task-kde-desktop task-lxde-desktop
task-xfce-desktop task-mate-desktop task-cinnamon-desktop xorg xorg-common
xserver-xorg xserver-xorg-core xserver-common xinit x11-common x11-apps x11-utils
x11-xserver-utils xauth xdg-utils dbus-x11 lightdm gdm3 sddm xdm slim xterm xvfb
x11vnc tigervnc-standalone-server xrdp chromium chromium-browser firefox-esr
fonts-noto-core xfonts-base gvfs thunar pcmanfm nautilus gedit libreoffice
alsa-utils pulseaudio bluez blueman"

# 判定包名是否属于桌面/图形组件 (0=是, 1=否)
pkg_gui_name() {
    local p="${1:-}" g
    [ -n "$p" ] || return 1
    for g in $APT_GUI_DENY; do
        if [ "$p" = "$g" ]; then return 0; fi
    done
    case "$p" in
        xserver-*|x11-*|task-*-desktop|xorg-*|*-desktop|fonts-*) return 0 ;;
    esac
    return 1
}

# 剔除参数中的桌面/图形组件: 剩余包打印到 stdout, 被剔除的写入全局变量 PKG_DROPPED
pkg_gui_filter() {
    local p out="" dropped=""
    PKG_DROPPED=""
    for p in "$@"; do
        [ -n "$p" ] || continue
        if pkg_gui_name "$p"; then
            dropped="${dropped}${dropped:+ }${p}"
        else
            out="${out}${out:+ }${p}"
        fi
    done
    # 告警必须走 stderr: 本函数的 stdout 会被命令替换当成装包清单收走
    if [ -n "$dropped" ]; then
        log_warn "已剔除桌面/图形组件 (目标机为无头服务器, 不安装): ${dropped}" >&2
    fi
    PKG_DROPPED="$dropped"
    printf '%s' "$out"
    return 0
}

# 可选包取值: 空 或 真值 (1/true/yes/on) -> 用预设清单; 否则按用户给的包名列表
extra_pkgs_apply() {
    case "$(printf '%s' "${1:-}" | tr 'A-Z' 'a-z')" in
        ''|1|true|yes|on) printf '%s' "$OPT_PKGS_PRESET" ;;
        *) printf '%s' "$1" ;;
    esac
}

# 计算最终装包清单 (核心 + 可选, 且已剔除桌面/图形组件), 结果写入 INSTALL_PKGS
# 按入参缓存: 配置摘要与真正安装两步都会用到它, 不缓存的话剔除告警会打印两次
pkg_install_list() {
    local key="${DO_APT_PKGS}|${DO_EXTRA_PKGS:-false}|${EXTRA_PKGS_REQUEST:-}"
    [ "${INSTALL_PKGS_KEY:-}" = "$key" ] && return 0
    INSTALL_PKGS_KEY="$key"
    INSTALL_EXTRA_PKGS=""
    if [ "$DO_APT_PKGS" != true ]; then
        INSTALL_PKGS=""
        return 0
    fi
    INSTALL_PKGS="$(pkg_gui_filter $BASE_PKGS)"
    if [ "${DO_EXTRA_PKGS:-false}" = true ]; then
        local _extra
        _extra="$(pkg_gui_filter $(extra_pkgs_apply "${EXTRA_PKGS_REQUEST:-}"))"
        if [ -n "$_extra" ]; then
            INSTALL_EXTRA_PKGS="$_extra"
            INSTALL_PKGS="${INSTALL_PKGS} ${_extra}"
        fi
    fi
    return 0
}

# ------------------------------------------------------------
# 发行版 / apt 辅助
# ------------------------------------------------------------
# 探测系统 ID 与代号, 输出 "<id> <codename>" (取不到则 unknown / 空)
# 刻意不 source /etc/os-release, 避免污染全局变量
# ONECLOUD_ETC_ROOT 仅用于测试/演练时把 /etc 指到别处 (默认 /etc)
detect_distro() {
    local _id="" _cn=""
    local osrel="${ONECLOUD_ETC_ROOT:-/etc}/os-release"
    if [ -r "$osrel" ]; then
        _id="$(sed -n 's/^ID=//p' "$osrel" 2>/dev/null | tr -d '"' | head -n 1 || true)"
        _cn="$(sed -n 's/^VERSION_CODENAME=//p' "$osrel" 2>/dev/null | tr -d '"' | head -n 1 || true)"
    fi
    if [ -z "$_cn" ] && command -v lsb_release >/dev/null 2>&1; then
        _cn="$(lsb_release -sc 2>/dev/null || true)"
    fi
    if [ -z "$_id" ] && command -v lsb_release >/dev/null 2>&1; then
        _id="$(lsb_release -si 2>/dev/null | tr '[:upper:]' '[:lower:]' || true)"
    fi
    echo "${_id:-unknown} ${_cn:-}"
}

# 系统代号 (bullseye / bookworm / ...); 取不到回退 bullseye
debian_codename_or_default() {
    local _d _cn
    _d="$(detect_distro)"
    _cn="${_d#* }"
    [ -n "$_cn" ] || _cn="bullseye"
    echo "$_cn"
}

# 不同代号可用组件不同: bookworm 起 firmware 拆到 non-free-firmware
apt_components_for() {
    case "$1" in
        bookworm|trixie|forky|sid|testing|unstable)
            echo "main contrib non-free non-free-firmware" ;;
        *)
            echo "main contrib non-free" ;;
    esac
}

# 5.6+ 内核已内置 wireguard 模块, 无需 dkms;
# 更老的内核才需要 wireguard-dkms (且该包只有 buster/backports 提供)
wireguard_kernel_builtin() {
    local kver k1 k2
    kver="$(uname -r)"
    k1="${kver%%.*}"
    k2="${kver#*.}"; k2="${k2%%.*}"
    if [ "${k1:-0}" -gt 5 ] 2>/dev/null; then return 0; fi
    if { [ "${k1:-0}" -eq 5 ] 2>/dev/null && [ "${k2:-0}" -ge 6 ] 2>/dev/null; }; then return 0; fi
    return 1
}

# 备份一次原始文件 (已备份则跳过)
apt_backup_once() {
    local f="$1"
    [ -f "$f" ] || return 0
    [ -f "${f}.onecloud.bak" ] && return 0
    cp -p "$f" "${f}.onecloud.bak" || true
}

# 注释掉 $1 里指向 Debian 源的 deb 行, 避免与 $2 (本次写入的源) 重复
apt_disable_debian_lines() {
    local f="$1" keep="$2"
    [ -f "$f" ] || return 0
    grep -qE '^[[:space:]]*deb[[:space:]]+[^[:space:]]*debian' "$f" 2>/dev/null || return 0
    apt_backup_once "$f"
    sed -i -E 's|^([[:space:]]*deb[[:space:]]+[^[:space:]]*debian.*)$|# [onecloud-disabled] \1|' "$f"
    log_warn "已注释 ${f} 中的 Debian 源行 (避免与 ${keep} 重复; 原文件备份为 ${f}.onecloud.bak)"
}

# ------------------------------------------------------------
# 写入 apt 源 —— 按系统实际代号渲染, 不再写死 bullseye
#   * 已存在 deb822 源 (Debian 12 默认 /etc/apt/sources.list.d/debian.sources)
#     -> 就地重写该文件
#   * 否则写经典 /etc/apt/sources.list
#   * 其余仍指向 Debian 的源文件一律注释/停用, 防止同一套源重复
#   * 被改动的文件都会留 <file>.onecloud.bak
# ------------------------------------------------------------
configure_apt_sources() {
    local codename="$1" mirror="$2" secmirror="$3"
    # ONECLOUD_ETC_ROOT 仅用于测试/演练时把 /etc 指到别处 (默认 /etc)
    local etc="${ONECLOUD_ETC_ROOT:-/etc}"
    local components deb822="${etc}/apt/sources.list.d/debian.sources"
    local classic="${etc}/apt/sources.list"
    local sources_d="${etc}/apt/sources.list.d"
    local keyring="/usr/share/keyrings/debian-archive-keyring.gpg"
    local f
    components="$(apt_components_for "$codename")"

    if [ -f "$deb822" ]; then
        APT_SOURCES_FILE="$deb822"
        apt_backup_once "$deb822"
        [ -f "$keyring" ] || log_warn "缺少 ${keyring}, deb822 源可能校验失败"
        cat > "$deb822" << EOF
# 由 bootstrap.sh 生成 — 镜像: ${mirror}
Types: deb
URIs: ${mirror}
Suites: ${codename} ${codename}-updates ${codename}-backports
Components: ${components}
Signed-By: ${keyring}

Types: deb
URIs: ${secmirror}
Suites: ${codename}-security
Components: ${components}
Signed-By: ${keyring}
EOF
        log_info "已写入 ${deb822} (deb822 格式)"
        apt_disable_debian_lines "$classic" "$deb822"
    else
        APT_SOURCES_FILE="$classic"
        apt_backup_once "$classic"
        cat > "$classic" << EOF
# 由 bootstrap.sh 生成 — 镜像: ${mirror}
deb ${mirror} ${codename} ${components}
deb ${mirror} ${codename}-updates ${components}
deb ${mirror} ${codename}-backports ${components}
deb ${secmirror} ${codename}-security ${components}
EOF
        log_info "已写入 ${classic} (经典格式)"
    fi

    for f in "${sources_d}"/*.list; do
        [ -f "$f" ] || continue
        [ "$f" = "$APT_SOURCES_FILE" ] && continue
        apt_disable_debian_lines "$f" "$APT_SOURCES_FILE"
    done

    if [ "$APT_SOURCES_FILE" != "$deb822" ]; then
        for f in "${sources_d}"/*.sources; do
            [ -f "$f" ] || continue
            grep -qE '^[[:space:]]*(URIs|deb)[[:space:]:]+[^[:space:]]*debian' "$f" 2>/dev/null || continue
            apt_backup_once "$f"
            mv "$f" "${f}.onecloud-disabled" || true
            log_warn "已停用重复的 Debian 源文件: ${f} -> ${f}.onecloud-disabled"
        done
    fi
}

# ------------------------------------------------------------
# 运行 apt 命令; 失败时打印真实报错与排查线索
# (历史上 apt 出错只透传退出码 100, 看不出到底哪一步/为什么失败)
# 注意: 失败时仍返回原退出码, 由调用方决定是致命还是继续
# ------------------------------------------------------------
apt_run() {
    local desc="$1"; shift
    local rc=0
    "$@" || rc=$?
    if [ "$rc" -ne 0 ]; then
        echo ""
        log_error "apt 步骤失败: ${desc}"
        log_error "  命令: $*"
        log_error "  退出码: ${rc}"
        if [ "$rc" -eq 100 ]; then
            log_error "  100 = apt/dpkg 处理失败 (源不可达 / 包不存在 / dpkg 锁被占用 / 依赖冲突)"
        fi
        log_error "  排查线索:"
        log_error "    * 源文件: ${APT_SOURCES_FILE:-未写入}  (系统代号: $(debian_codename_or_default))"
        log_error "    * dpkg 锁: fuser -v /var/lib/dpkg/lock-frontend"
        log_error "    * 手工复核: apt update; apt install -y <包名>"
        return "$rc"
    fi
    return 0
}

# 不致命的 apt 步骤: 失败只告警, 不让整机初始化中断
apt_try() {
    local desc="$1"; shift
    if ! apt_run "$desc" "$@"; then
        log_warn "该步骤失败但继续: ${desc}"
    fi
    return 0
}

# ------------------------------------------------------------
# DNS 取值: 区分"自动获取 (DHCP)"与"写死地址列表"
# ------------------------------------------------------------
# 以下写法一律视为自动获取: 空 / dhcp / auto / none / automatic / 自动获取
dns_is_auto() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]' | tr -d ' ')" in
        ''|dhcp|auto|none|automatic|自动|自动获取) return 0 ;;
        *) return 1 ;;
    esac
}

# 依候选值设置 DNS_MODE (dhcp|static) 与 DNS_SERVERS
# 自动模式下清空 DNS_SERVERS, 保证后续不会把 dhcp 当成 nameserver 写进配置
dns_apply_mode() {
    if dns_is_auto "${1:-}"; then
        DNS_MODE="dhcp"
        DNS_SERVERS=""
    else
        DNS_MODE="static"
        DNS_SERVERS="$1"
    fi
}

# ------------------------------------------------------------
# apt 动作开关 —— 换源与更新默认都跳过
#
#   历史行为: 初始化必然换源 + 必然 apt update/upgrade。现场代价不小:
#     换源失败会留下半截源文件; 升级可能拉入新内核/firmware 让机器起不来;
#     多数节点跑这个脚本前源和索引其实已经就绪, 再动一遍纯属引入变量。
#   现在四项各自独立、默认都不做, 需要哪项显式开哪项:
#
#     换源       DO_MIRROR       --mirror       / ONECLOUD_APT_ENABLE_MIRROR=1
#     刷新索引   DO_APT_UPDATE   --apt-update   / ONECLOUD_APT_ENABLE_UPDATE=1
#     升级系统包 DO_APT_UPGRADE  --apt-upgrade  / ONECLOUD_APT_ENABLE_UPGRADE=1
#     装基础工具 DO_APT_PKGS     (默认开)       / --no-apt-pkgs 关闭
#     装可选工具 DO_EXTRA_PKGS   --extra-pkgs   / ONECLOUD_EXTRA_PKGS (默认关)
#
#   --no-apt 一键关掉全部 (纯离线初始化: 只配主机名/IP/存储/目录/SSH 密钥)
#   旧开关 ONECLOUD_APT_SKIP_MIRROR=1 继续有效 (= 不换源, 向后兼容)
# ------------------------------------------------------------
apt_switch_defaults() {
    DO_MIRROR=false
    DO_APT_UPDATE=false
    DO_APT_UPGRADE=false
    DO_APT_PKGS=true
    APT_UPDATE_AUTO=false        # 换源后自动补的刷索引 (不是用户本意, 日志里要说明)
    APT_UPDATE_EXPLICIT=false    # 用户是否显式表过态 (显式优先于自动补)
    APT_OPTS_EXPLICIT=false      # 命令行是否给过任一 apt 开关 (给了就不再交互询问)
    # 可选工具: 默认不装。ONECLOUD_EXTRA_PKGS 给真值 (1/true/yes/on) 用预设清单,
    # 给包名列表则按列表装; 见 extra_pkgs_apply()
    DO_EXTRA_PKGS=false
    EXTRA_PKGS_REQUEST=""
    if [ -n "${ONECLOUD_EXTRA_PKGS:-}" ]; then
        DO_EXTRA_PKGS=true
        EXTRA_PKGS_REQUEST="$ONECLOUD_EXTRA_PKGS"
    fi
    case "$(printf '%s' "${ONECLOUD_APT_ENABLE_MIRROR:-}" | tr 'A-Z' 'a-z')" in
        1|true|yes|on) DO_MIRROR=true ;;
    esac
    case "$(printf '%s' "${ONECLOUD_APT_ENABLE_UPDATE:-}" | tr 'A-Z' 'a-z')" in
        1|true|yes|on) DO_APT_UPDATE=true ;;
    esac
    case "$(printf '%s' "${ONECLOUD_APT_ENABLE_UPGRADE:-}" | tr 'A-Z' 'a-z')" in
        1|true|yes|on) DO_APT_UPGRADE=true ;;
    esac
    # 兼容旧开关与一键开关, 放最后 (优先级最高)
    case "$(printf '%s' "${ONECLOUD_APT_SKIP_MIRROR:-}" | tr 'A-Z' 'a-z')" in
        1|true|yes|on) DO_MIRROR=false ;;
    esac
    case "$(printf '%s' "${ONECLOUD_APT_SKIP_PKGS:-}" | tr 'A-Z' 'a-z')" in
        1|true|yes|on) DO_APT_PKGS=false ;;
    esac
    case "$(printf '%s' "${ONECLOUD_APT_SKIP_ALL:-}" | tr 'A-Z' 'a-z')" in
        1|true|yes|on)
            DO_MIRROR=false; DO_APT_UPDATE=false; DO_APT_UPGRADE=false; DO_APT_PKGS=false
            DO_EXTRA_PKGS=false; EXTRA_PKGS_REQUEST="" ;;
    esac
    return 0
}

# 换源之后索引必须跟着刷新, 否则 apt install 会拿旧索引去新源取包 -> 404。
# 因此「只换了源、没表态要不要更新」时自动补一步 apt update (不含 upgrade)。
apt_switch_fixup() {
    if [ "$DO_MIRROR" = true ] && [ "$DO_APT_UPDATE" != true ] \
       && [ "$APT_UPDATE_EXPLICIT" != true ]; then
        DO_APT_UPDATE=true
        APT_UPDATE_AUTO=true
    fi
    return 0
}

# 四项状态摘要 (dry-run 与配置汇总共用); 只写 stdout
apt_switch_desc() {
    if [ "$DO_MIRROR" = true ]; then
        printf '    换源      : 执行 (镜像 %s)\n' \
            "${ONECLOUD_APT_MIRROR:-https://mirrors.tuna.tsinghua.edu.cn/debian}"
    else
        printf '    换源      : 跳过 (默认; 需要时加 --mirror)\n'
    fi
    if [ "$DO_APT_UPDATE" = true ]; then
        if [ "$APT_UPDATE_AUTO" = true ]; then
            printf '    刷新索引  : 执行 (换源后自动补, 可用 --no-apt-update 关闭)\n'
        else
            printf '    刷新索引  : 执行 (--apt-update)\n'
        fi
    else
        printf '    刷新索引  : 跳过 (默认; 需要时加 --apt-update)\n'
    fi
    if [ "$DO_APT_UPGRADE" = true ]; then
        printf '    升级系统  : 执行 (--apt-upgrade)\n'
    else
        printf '    升级系统  : 跳过 (默认; 需要时加 --apt-upgrade)\n'
    fi
    if [ "$DO_APT_PKGS" = true ]; then
        pkg_install_list
        printf '    装包清单  : %s\n' "${INSTALL_PKGS}"
        if [ "${DO_EXTRA_PKGS:-false}" != true ]; then
            printf '    可选工具  : 跳过 (默认; 需要时加 --extra-pkgs)\n'
        fi
    else
        printf '    装包清单  : 跳过 (--no-apt-pkgs)\n'
    fi
    return 0
}

# 四项全关 (= 完全不动 apt)
apt_switch_all_off() {
    [ "$DO_MIRROR" != true ] && [ "$DO_APT_UPDATE" != true ] \
        && [ "$DO_APT_UPGRADE" != true ] && [ "$DO_APT_PKGS" != true ]
}

# ------------------------------------------------------------
# 将本次部署选定的节点身份 upsert 到 inventory/nodes.local.yaml
# (该文件不入库; 面板/部署/备份脚本都会读取它作为覆盖层)
# 用法: update_local_inventory <节点名> <IP> <主机名> [WG_IP]
# 返回 0=已写入; 1=失败。幂等: 已存在则就地更新字段, 不存在则追加到 nodes: 列表。
# ------------------------------------------------------------
update_local_inventory() {
    local node="$1" ip="$2" host="$3" wg="${4:-}"
    [ -n "$node" ] || return 1
    local proj="${SCRIPT_DIR%/scripts}"
    local dir="${proj}/inventory"
    local f="${dir}/nodes.local.yaml"
    mkdir -p "$dir" 2>/dev/null || return 1

    if [ ! -f "$f" ]; then
        {
            echo "# 由 scripts/bootstrap.sh 自动维护 (不入库)"
            echo "# 记录本机部署时选定的节点身份; 控制端面板/部署脚本读取此覆盖层。"
            echo "nodes:"
        } > "$f" 2>/dev/null || return 1
    fi

    local tmp="${f}.tmp.$$"
    awk -v node="$node" -v ip="$ip" -v host="$host" -v wg="$wg" '
    function emit() {
        print "  - name: " node
        if (host != "") print "    hostname: " host
        if (ip   != "") print "    ip: " ip
        if (wg   != "") print "    wg_ip: " wg
    }
    BEGIN { inblk = 0; found = 0; inserted = 0; in_nodes = 0 }
    {
        line = $0
        if (inblk == 1) {
            # 块结束: 遇新的列表项或顶层键
            if (line ~ /^[[:space:]]*-[[:space:]]*name:/ || line ~ /^[A-Za-z_]/) {
                inblk = 0
            } else {
                next    # 吞掉旧块内的字段
            }
        }
        if (line ~ ("^[[:space:]]*-[[:space:]]*name:[[:space:]]*" node "[[:space:]]*$")) {
            emit(); found = 1; inserted = 1; inblk = 1; next
        }
        if (line ~ /^[A-Za-z_]/) {
            # 顶层键: 离开 nodes 段前, 若新节点尚未写入则补在 nodes 段末尾
            if (in_nodes == 1 && inserted == 0 && found == 0 && ip != "") { emit(); inserted = 1 }
            if (line ~ /^nodes:[[:space:]]*$/) in_nodes = 1
            else in_nodes = 0
            print; next
        }
        print
    }
    END { if (inserted == 0 && found == 0 && ip != "") emit() }' \
        "$f" > "$tmp" 2>/dev/null || { rm -f "$tmp"; return 1; }

    mv "$tmp" "$f" 2>/dev/null || { rm -f "$tmp"; return 1; }
    return 0
}

# ------------------------------------------------------------
# 网段计算: 由 IP + 前缀得到 "网络地址 网关"
# 网关取网络地址 + 1 (家用网段惯例), 输出 "<网络地址> <网关>"
# 纯算术实现, 不依赖 awk 的位运算函数 (mawk 没有 and()/lshift())
# ------------------------------------------------------------
ip_net_info() {
    awk -v ip="$1" -v prefix="${2:-24}" 'BEGIN {
        if (ip !~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/) exit 1
        if (split(ip, o, ".") != 4) exit 1
        for (i = 1; i <= 4; i++) if (o[i] + 0 < 0 || o[i] + 0 > 255) exit 1
        if (prefix !~ /^[0-9]+$/ || prefix + 0 < 1 || prefix + 0 > 32) prefix = 24
        v = o[1]*16777216 + o[2]*65536 + o[3]*256 + o[4]
        size = 2 ^ (32 - prefix)
        if (size < 4) size = 4
        net = int(v / size) * size
        gw  = net + 1
        printf "%d.%d.%d.%d %d.%d.%d.%d\n", \
            int(net/16777216)%256, int(net/65536)%256, int(net/256)%256, net%256, \
            int(gw /16777216)%256, int(gw /65536)%256, int(gw /256)%256, gw %256
    }'
}

# 取某个 IP 的网络地址 (用于判断两个地址是否同网段)
ip_net_addr() {
    local _info
    if _info="$(ip_net_info "$1" "${2:-24}")"; then
        echo "${_info%% *}"
    else
        return 1
    fi
}

# ------------------------------------------------------------
# 探测本机当前网络: 主网卡 IP / 前缀 / 默认网关
# 结果写入 CUR_IP CUR_PREFIX CUR_GW (拿不到则为空)
# ------------------------------------------------------------
detect_current_network() {
    CUR_IP=""; CUR_PREFIX=""; CUR_GW=""
    local _cidr
    if command -v ip >/dev/null 2>&1; then
        _cidr="$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4; exit}')" || true
        if [ -n "$_cidr" ]; then
            CUR_IP="${_cidr%%/*}"
            CUR_PREFIX="${_cidr##*/}"
        fi
        CUR_GW="$(ip route show default 2>/dev/null \
                  | awk '{for (i = 1; i <= NF; i++) if ($i == "via") {print $(i+1); exit}}')" || true
    fi
    # 兜底: net-tools 的 route
    if [ -z "$CUR_GW" ] && command -v route >/dev/null 2>&1; then
        CUR_GW="$(route -n 2>/dev/null | awk '$1 == "0.0.0.0" {print $2; exit}')" || true
    fi
    if [ -z "$CUR_IP" ] && command -v hostname >/dev/null 2>&1; then
        CUR_IP="$(hostname -I 2>/dev/null | awk '{print $1}')" || true
    fi
    case "$CUR_PREFIX" in
        ''|*[!0-9]*) CUR_PREFIX="" ;;
    esac
    # 过滤掉明显无效的值
    case "$CUR_IP" in
        ''|*[!0-9.]*) CUR_IP="" ;;
    esac
    case "$CUR_GW" in
        ''|*[!0-9.]*) CUR_GW="" ;;
    esac
}

# ------------------------------------------------------------
# 探测可移动存储 (SD 卡 / U 盘): 结果写入 SD_CANDIDATES 数组
# 判据 (任一满足即视为候选): lsblk removable=1 / 传输层 usb /
# 非根分区的 mmcblk 设备 (玩客云 eMMC 常为 mmcblk0, SD 为 mmcblk1)
# ------------------------------------------------------------
DETECTED_SD_REASON=""
detect_sd_cards() {
    SD_CANDIDATES=()
    DETECTED_SD_REASON=""
    local _root_src _root_dev _name _rm _type _tran _d
    _root_src="$(findmnt -n -o SOURCE / 2>/dev/null | awk '{print $1}')" || true
    _root_dev="$(basename "${_root_src:-}" 2>/dev/null || true)"
    # 分区后缀处理: mmcblk0p1 -> mmcblk0 ; sda1 -> sda
    case "$_root_dev" in
        mmcblk*p[0-9]*)     _root_dev="${_root_dev%p[0-9]*}" ;;
        [shv]d[a-z][0-9]*)  _root_dev="${_root_dev%[0-9]}" ;;
    esac

    # 第一轮: 高置信度 (可移动标记 / USB 通道)
    if command -v lsblk >/dev/null 2>&1; then
        while read -r _name _rm _type _tran; do
            [ -n "$_name" ] || continue
            [ "$_type" = "disk" ] || continue
            case "$_name" in
                mmcblk*|sd[a-z]) ;;
                *) continue ;;
            esac
            if [ "$_name" = "$_root_dev" ]; then continue; fi
            if [ "$_rm" = "1" ]; then
                SD_CANDIDATES+=("$_name"); DETECTED_SD_REASON="可移动设备"
            elif [ "$_tran" = "usb" ]; then
                SD_CANDIDATES+=("$_name"); DETECTED_SD_REASON="USB 存储"
            fi
        done < <(lsblk -dno NAME,RM,TYPE,TRAN 2>/dev/null || true)
    fi

    # 第二轮: /sys/block 兜底 (无 lsblk 的精简系统)
    if [ "${#SD_CANDIDATES[@]}" -eq 0 ] && [ -d /sys/block ]; then
        for _d in /sys/block/*; do
            [ -d "$_d" ] || continue
            _name="$(basename "$_d")"
            case "$_name" in
                mmcblk*|sd[a-z]) ;;
                *) continue ;;
            esac
            if [ "$_name" = "$_root_dev" ]; then continue; fi
            if [ "$(cat "$_d/removable" 2>/dev/null || echo 0)" = "1" ]; then
                SD_CANDIDATES+=("$_name"); DETECTED_SD_REASON="可移动设备"
            fi
        done
    fi

    # 第三轮: 低置信度启发式 —— 非根分区的 mmcblk 视为 SD
    # (玩客云 eMMC 一般是 mmcblk0, 插卡为 mmcblk1; 仅在无更可靠线索时启用)
    if [ "${#SD_CANDIDATES[@]}" -eq 0 ] && [ -d /sys/block ]; then
        for _d in /sys/block/*; do
            [ -d "$_d" ] || continue
            _name="$(basename "$_d")"
            case "$_name" in
                mmcblk*) ;;
                *) continue ;;
            esac
            if [ "$_name" = "$_root_dev" ]; then continue; fi
            SD_CANDIDATES+=("$_name"); DETECTED_SD_REASON="非系统 eMMC/SD (启发式)"
        done
    fi
}

# ping 可达性探测 (兼容 Linux/Windows 两套参数)
ping_ok() {
    local _target="$1" _count="${2:-1}"
    [ -n "$_target" ] || return 1
    command -v ping >/dev/null 2>&1 || return 1
    if ping -c "$_count" -W 2 "$_target" >/dev/null 2>&1; then
        return 0
    fi
    ping -n "$_count" -w 2000 "$_target" >/dev/null 2>&1
}

usage() {
    cat << EOF
用法: $0 [选项]

选项:
  -n, --node NAME       节点名称 (如 wk-edge-01)
  -i, --ip IP           静态 IP (清单中已登记的节点可省略)
  -H, --hostname NAME   主机名 (默认: 清单值, 或节点名去掉 wk- 前缀)
  -g, --gateway IP      网关 (默认: 由最终 IP 推导)
  -D, --dns IP|dhcp     DNS: 具体地址 (多个用逗号分隔) 或 dhcp (自动获取)
      --dns-dhcp        等价于 --dns dhcp: 不写死 DNS, 交由 DHCP/系统提供
  -s, --sd DEV          SD 卡设备名 (如 mmcblk1, 不含 /dev/; 默认自动探测)
  -m, --sd-mount DIR    SD 卡挂载点 (默认 /mnt/sd)
      --no-sd           完全跳过 SD 卡挂载与 Docker 数据迁移
      --no-sd-automount 挂载 SD 卡但不写入 fstab (不自动挂载)
  -y, --yes             跳过交互确认 (非交互/自动化场景必填)
      --dry-run         只打印将要应用的配置, 不修改系统 (可单独使用)
      --no-detect       不自动采用本机探测到的 IP/网关 (仍会探测并用于风险提示)

  apt 动作 (四项独立, 默认全部跳过):
      --mirror          替换 apt 源为国内镜像
      --no-mirror       不换源 (默认行为, 显式写出便于脚本自述)
      --apt-update      刷新 apt 索引 (apt update)
      --no-apt-update   不刷新索引 (默认行为)
      --apt-upgrade     升级系统包 (apt upgrade -y; 默认: 跳过)
      --no-apt-upgrade  不升级系统包 (默认行为)
      --no-apt-pkgs     跳过基础工具安装 (curl/git/parted/wireguard-tools ...)
      --extra-pkgs[=..] 安装可选工具 (默认不装)。不带值=预设清单, 带值=指定包名
      --no-extra-pkgs   不装可选工具 (默认行为)
      --no-apt          跳过以上全部 (纯离线初始化: 只配主机名/IP/存储/目录/密钥)
  -h, --help            显示帮助

初始化装包范围 (无头服务器, 不装任何桌面/图形组件):
  核心 (默认装)  curl git ca-certificates jq rsync parted wireguard-tools
  可选 (默认不装) wget vim htop iotop net-tools dnsutils unzip dosfstools
                 fdisk lsb-release gnupg        -- 需要时加 --extra-pkgs
  桌面/图形 (永不装) 桌面套件 / Xorg / 显示管理器 / 字体 / 浏览器 / 远程桌面等,
                 即便显式列出也会被剔除并告警 (见 APT_GUI_DENY)
  --extra-pkgs 用法: --extra-pkgs            (装上面那份可选预设)
                     --extra-pkgs "vim htop" (只装指定的)

参数与询问的关系:
  传入的参数一律直接生效, 不会被询问覆盖;
  只有"未提供且无法从清单/探测推断"的项, 才在终端可用时交互询问。
  非交互环境 (cron/CI) 请配合 --yes, 否则缺失项会直接报错而不是干等输入。

网络取值优先级:
  IP     命令行 --ip  >  本机探测(询问/--yes 采用)  >  清单
  网关   命令行 --gateway  >  由最终 IP 推导  >  本机探测  >  清单
  DNS    命令行 --dns  >  环境变量 ONECLOUD_DNS  >  清单 network.dns  >  默认 dhcp
         (取值写 dhcp / auto / none 均表示"自动获取", 交互时直接回车也是它)

DNS 的两种模式:
  自动获取 (dhcp, 默认)  不向系统写入任何 nameserver, 交给 DHCP / 网络管理器 / 系统现状;
                         交互询问直接回车即为该模式。注意: 本脚本配置的是静态 IP,
                         若系统没有其它 DNS 来源(如已停跑的 dhclient), 解析可能失败 ——
                         此时重新执行并指定 --dns <你的DNS> 即可。
  静态指定 (IP 列表)     写入 dns-nameservers / netplan nameservers, 最可预期。

执行前的安全检查 (防止配完静态 IP 后失联):
  * 新 IP 与本机当前 IP 不同网段 -> 告警并要求确认
  * 新 IP 已被占用 (ping 有响应) -> 告警并要求确认
  * 网关 ping 不可达 -> 告警并要求确认
  * 当前是 SSH 会话且 IP 将变更 -> 告警 (这条连接会当场断开)
  * 防火墙现状未放行 SSH 端口 -> 告警 (改完可能再也登不回来)
  --yes 下仅告警后继续; --dry-run 下只提示不改动。

通路自检 (只读, 不改系统):
  启动时会打印当前会话类型、sshd 端口、防火墙后端与 INPUT/FORWARD 默认策略。
  本脚本自身不触碰防火墙 —— 唯一会写 iptables 的是 WireGuard 的 PostUp/PostDown。
  常见"被自己挡在门外"的情形都会在这里点出来, 不会自动修改:
    * INPUT 默认策略为 DROP 而没放行 SSH 端口
    * ufw / firewalld 启用后未放行 SSH
    * Docker 启动会把 FORWARD 置 DROP, 若本机是 WireGuard 出口会顺带打断转发

改 IP 前的自保动作:
  写网络配置前把 network/ 与 netplan/ 打成快照 (.onecloud/net-backup-<时间戳>/),
  校验不过就不 apply; 通过 SSH 远端操作时会打印重新登录与回滚命令。

环境变量 (优先级最高):
  ONECLOUD_WK_EDGE_01_IP / _HOSTNAME / _WG_IP
  ONECLOUD_GATEWAY / ONECLOUD_DNS / ONECLOUD_LAN_SUBNET / ONECLOUD_DOMAIN
                                                      # ONECLOUD_DNS=dhcp 亦可 (自动获取)
  ONECLOUD_APT_ENABLE_MIRROR=1                        # 等价于 --mirror
  ONECLOUD_APT_ENABLE_UPDATE=1                        # 等价于 --apt-update
  ONECLOUD_APT_ENABLE_UPGRADE=1                       # 等价于 --apt-upgrade
  ONECLOUD_APT_SKIP_PKGS=1                            # 跳过基础工具安装
  ONECLOUD_EXTRA_PKGS="vim htop"                      # 装可选工具 (= --extra-pkgs;
                                                      #   取 1/true/yes/on 则用预设清单)
  ONECLOUD_APT_SKIP_ALL=1                             # 跳过全部 apt 动作
  ONECLOUD_APT_SKIP_MIRROR=1                          # 旧开关, 仍然有效 (= 不换源)
  ONECLOUD_APT_MIRROR / ONECLOUD_APT_SECURITY_MIRROR  # 覆盖默认 apt 镜像
  ONECLOUD_DEBIAN_CODENAME                            # 强制指定 apt 源代号
  ONECLOUD_ETC_ROOT                                   # 测试用: 把 /etc 指到别处 (默认 /etc)

apt 源说明:
  换源默认不做, 需要时 --mirror。开启后源按系统实际代号 (/etc/os-release 的
  VERSION_CODENAME) 渲染, 不写死 bullseye; 系统已用 deb822 格式
  (/etc/apt/sources.list.d/debian.sources) 时就地重写该文件, 其余仍指向 Debian
  的源会被注释/停用以免重复。被改动的文件都留 .onecloud.bak。
  只换源未表态更新时, 会自动补一步 apt update (新源配旧索引装包必 404)。

示例:
  $0 --node wk-edge-01 --yes                                  # 全部取清单值, 不动 apt
  $0 --node wk-edge-01 --ip 10.0.0.5 --hostname edge-01 --yes  # 覆盖 IP 与主机名
  $0 --node wk-new --ip 10.0.0.9 --hostname new --gateway 10.0.0.1 --yes
  $0 --node wk-edge-01 --yes --mirror                         # 顺便换源 (自动刷索引)
  $0 --node wk-edge-01 --yes --no-apt                         # 一个字节都不动 apt
  $0 --node wk-edge-01 --yes --extra-pkgs                     # 另外装可选工具 (vim/htop/...)
  $0 --node wk-edge-01 --yes --extra-pkgs "vim iotop"         # 只装指定的可选工具
  $0                                                          # 无参数: 全交互询问
EOF
}

echo ""
echo "=========================================="
echo "  OneCloud Cluster - Node Bootstrap"
echo "=========================================="
echo ""

# 帮助优先: 直接输出用法, 不做任何探测
for _a in "$@"; do
    case "$_a" in
        -h|--help) usage; exit 0 ;;
    esac
done

# ============================================================
# 0. 立即探测本机现状 (IP / 网关 / 可移动存储)
#    放在最前, 供后续网段比对与风险提示使用; 只读, 不改系统
# ============================================================
detect_current_network
if [ -n "$CUR_IP" ] || [ -n "$CUR_GW" ]; then
    log_info "本机当前网络: IP=${CUR_IP:-未获取}${CUR_PREFIX:+/$CUR_PREFIX} 网关=${CUR_GW:-未获取}"
else
    log_warn "未能探测到本机当前 IP/网关 (缺少 ip 命令或网络未就绪)"
fi
detect_sd_cards
if [ "${#SD_CANDIDATES[@]}" -gt 0 ]; then
    log_info "检测到可移动存储: /dev/${SD_CANDIDATES[*]} (${DETECTED_SD_REASON})"
else
    log_info "未检测到 SD 卡 / USB 存储"
fi

# ============================================================
# 0b. 通路自检: 防火墙 / SSH 放行 / 转发策略
#     纯只读, 不修改任何系统状态。目的是在"开始改网络"之前先把
#     已经存在的、可能把人挡在门外的东西说出来 (INPUT 策略 DROP、
#     ufw 未放行 SSH、Docker 把 FORWARD 置 DROP 之类)。
# ============================================================
NET_AUDIT_RISK=false
if [ "$HAVE_NET_AUDIT" = true ]; then
    echo ""
    if ! net_audit_report; then
        NET_AUDIT_RISK=true
        log_warn "自检发现可能阻断 SSH 通道或转发的现状, 请先确认再继续"
    fi
    echo ""
fi

NODE_NAME=""
NODE_IP=""
HOSTNAME=""
SD_DEV=""
GATEWAY=""
DNS_SERVERS=""
DNS_EXPLICIT=false          # 命令行是否显式指定了 DNS
DNS_MODE="dhcp"             # dhcp = 自动获取 (默认); static = 写死指定地址
SD_MOUNT="/mnt/sd"
ASSUME_YES=false
NO_DETECT=false
DRY_RUN=false
NO_SD=false
NO_SD_AUTOMOUNT=false

# apt 动作开关初值 (默认全部跳过; 环境变量可覆盖; 命令行参数在下面再覆盖一次)
apt_switch_defaults

while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|--node)      NODE_NAME="$2";    shift 2 ;;
        -i|--ip)        NODE_IP="$2";      shift 2 ;;
        -H|--hostname)  HOSTNAME="$2";     shift 2 ;;
        -g|--gateway)   GATEWAY="$2";      shift 2 ;;
        -D|--dns)       DNS_SERVERS="$2";  DNS_EXPLICIT=true; shift 2 ;;
        --dns-dhcp)     DNS_SERVERS="dhcp"; DNS_EXPLICIT=true; shift ;;
        -s|--sd)        SD_DEV="$2";       shift 2 ;;
        -m|--sd-mount)  SD_MOUNT="$2";     shift 2 ;;
        --no-sd)        NO_SD=true;        shift ;;
        --no-sd-automount) NO_SD_AUTOMOUNT=true; shift ;;
        --mirror)       DO_MIRROR=true;    APT_OPTS_EXPLICIT=true; shift ;;
        --no-mirror)    DO_MIRROR=false;   APT_OPTS_EXPLICIT=true; shift ;;
        --apt-update)   DO_APT_UPDATE=true;   APT_UPDATE_EXPLICIT=true
                        APT_OPTS_EXPLICIT=true; shift ;;
        --no-apt-update) DO_APT_UPDATE=false; APT_UPDATE_EXPLICIT=true
                        APT_OPTS_EXPLICIT=true; shift ;;
        --apt-upgrade)  DO_APT_UPGRADE=true;  APT_OPTS_EXPLICIT=true; shift ;;
        --no-apt-upgrade) DO_APT_UPGRADE=false; APT_OPTS_EXPLICIT=true; shift ;;
        --no-apt-pkgs)  DO_APT_PKGS=false;  APT_OPTS_EXPLICIT=true; shift ;;
        --extra-pkgs)   DO_EXTRA_PKGS=true; APT_OPTS_EXPLICIT=true
                        # 可带值 (包名列表); 不带值或下一个参数是选项 -> 用预设清单
                        if [ $# -ge 2 ] && [ -n "${2:-}" ] && [ "${2#-}" = "$2" ]; then
                            EXTRA_PKGS_REQUEST="$2"; shift 2
                        else
                            EXTRA_PKGS_REQUEST=""; shift
                        fi ;;
        --no-extra-pkgs) DO_EXTRA_PKGS=false; EXTRA_PKGS_REQUEST=""
                        APT_OPTS_EXPLICIT=true; shift ;;
        --no-apt)       DO_MIRROR=false; DO_APT_UPDATE=false
                        DO_APT_UPGRADE=false; DO_APT_PKGS=false
                        DO_EXTRA_PKGS=false; EXTRA_PKGS_REQUEST=""
                        APT_OPTS_EXPLICIT=true; shift ;;
        -y|--yes)       ASSUME_YES=true;   shift ;;
        --dry-run)      DRY_RUN=true;      shift ;;
        --no-detect)    NO_DETECT=true;    shift ;;
        -h|--help)      usage; exit 0 ;;
        *) log_error "未知选项: $1"; usage; exit 1 ;;
    esac
done

# 交互判定: 默认看是否有 TTY; 自动化场景可用 ONECLOUD_BOOTSTRAP_TTY=1/0 强制指定
if [ -n "${ONECLOUD_BOOTSTRAP_TTY:-}" ]; then
    if [ "${ONECLOUD_BOOTSTRAP_TTY}" = "1" ]; then
        INTERACTIVE=true
    else
        INTERACTIVE=false
    fi
elif [ -t 0 ]; then
    INTERACTIVE=true
else
    INTERACTIVE=false
fi
# 参数已给定或 --yes 时不必交互
if [ "$ASSUME_YES" = true ] || [ "$DRY_RUN" = true ]; then
    INTERACTIVE=false
fi

DETECT_IP="$CUR_IP"; DETECT_GW="$CUR_GW"; DETECT_PREFIX="$CUR_PREFIX"

# ---- 1a0. apt 动作选择 (换源/更新默认都跳过) ----
# 只在"没人表过态 + 当前可交互"时问; 一律默认 N, 直接回车 = 一个字节都不动 apt。
# 非交互场景 (cron/CI) 请用 --mirror / --apt-update 或 ONECLOUD_APT_* 环境变量。
if [ "$INTERACTIVE" = true ] && [ "$APT_OPTS_EXPLICIT" != true ]; then
    echo ""
    log_info "apt 动作 (默认全部跳过; 直接回车即跳过, 也可稍后用参数指定)"
    _ans_mirror=""
    read -r -p "  替换 apt 源为国内镜像? [y/N] " _ans_mirror || true
    case "$_ans_mirror" in
        [Yy]*) DO_MIRROR=true ;;
        *)     DO_MIRROR=false ;;
    esac
    _ans_update=""
    read -r -p "  更新系统包 (apt update && apt upgrade -y)? [y/N] " _ans_update || true
    case "$_ans_update" in
        [Yy]*) DO_APT_UPDATE=true; DO_APT_UPGRADE=true ;;
        *)     DO_APT_UPDATE=false; DO_APT_UPGRADE=false ;;
    esac
    if [ "$DO_APT_PKGS" = true ] && [ "$DO_MIRROR" != true ] && [ "$DO_APT_UPDATE" != true ]; then
        log_warn "基础工具仍会尝试安装 (未刷新索引, 失败只告警); 需一并跳过请加 --no-apt-pkgs"
    fi
fi
# 换源了却没说要更新 -> 自动补一步刷新索引 (否则新源配旧索引, 装包必 404)
apt_switch_fixup

# ---- 1a. 用清单填充未显式给出的参数 (清单只作为默认值, 命令行优先) ----
INVENTORY_HIT=false
IP_SOURCE="命令行"
[ -z "$NODE_IP" ] && IP_SOURCE=""
GW_SOURCE="命令行"
[ -z "$GATEWAY" ] && GW_SOURCE=""
if [ "$HAVE_INVENTORY" = true ] && [ -n "$NODE_NAME" ]; then
    if RESOLVED="$(node_resolve "$NODE_NAME" 2>/dev/null)"; then
        INVENTORY_HIT=true
        NODE_NAME="$RESOLVED"
        if [ -z "$NODE_IP" ]; then
            NODE_IP="$(node_ip "$RESOLVED")"
            IP_SOURCE="清单"
        fi
        if [ -z "$HOSTNAME" ]; then
            HOSTNAME="$(node_hostname "$RESOLVED")"
        fi
    fi
fi

# 网络参数: 命令行 > 环境变量 > 清单
if [ -z "$GATEWAY" ]; then
    GATEWAY="${ONECLOUD_GATEWAY:-${NET_GATEWAY:-}}"
    if [ -n "$GATEWAY" ]; then
        GW_SOURCE="清单"
    else
        GW_SOURCE=""
    fi
fi
# DNS 取值: 命令行 --dns > ONECLOUD_DNS > 清单 network.dns > 默认 dhcp(自动获取)
DNS_SOURCE=""
DNS_RAW="$DNS_SERVERS"
if [ "$DNS_EXPLICIT" = true ]; then
    DNS_SOURCE="命令行"
elif [ -n "${ONECLOUD_DNS:-}" ]; then
    DNS_RAW="$ONECLOUD_DNS"; DNS_SOURCE="环境变量"
elif [ -n "${NET_DNS:-}" ]; then
    DNS_RAW="$NET_DNS"; DNS_SOURCE="清单"
else
    DNS_RAW=""; DNS_SOURCE="默认"
fi
dns_apply_mode "$DNS_RAW"
if [ "$DNS_MODE" = "dhcp" ] && [ "$DNS_SOURCE" = "默认" ]; then
    DNS_SOURCE="默认(自动获取)"
fi

# 前缀: 本机探测 > 清单 > 24
if [ -n "$DETECT_PREFIX" ]; then
    LAN_PREFIX="$DETECT_PREFIX"
else
    LAN_PREFIX="${NET_LAN_PREFIX:-24}"
fi

# ---- 1b. 未指定 IP 时: 询问是否直接采用本机当前 IP / 网关 ----
if [ -z "$NODE_IP" ] && [ -n "$DETECT_IP" ] && [ "$NO_DETECT" != true ]; then
    _take_ip=""
    if [ "$ASSUME_YES" = true ]; then
        _take_ip="y"
    elif [ "$INTERACTIVE" = true ]; then
        read -r -p "是否直接使用本机当前 IP ${DETECT_IP}? [Y/n] " _use_cur || true
        if [[ ! "$_use_cur" =~ ^[Nn]$ ]]; then
            _take_ip="y"
        fi
    fi
    if [ -n "$_take_ip" ]; then
        NODE_IP="$DETECT_IP"
        IP_SOURCE="本机探测"
        log_info "未指定 --ip, 已采用本机当前 IP ${NODE_IP}"
        # 网关: 命令行 --gateway 显式指定时优先, 否则一并采用本机当前网关
        if [ -n "$DETECT_GW" ] && [ "$GW_SOURCE" != "命令行" ]; then
            if [ -z "$GATEWAY" ] || [ "$GATEWAY" = "$DETECT_GW" ] || [ "$ASSUME_YES" = true ]; then
                GATEWAY="$DETECT_GW"
                GW_SOURCE="本机探测"
            elif [ "$INTERACTIVE" = true ]; then
                read -r -p "是否同时使用本机当前网关 ${DETECT_GW} (现为 ${GATEWAY})? [Y/n] " _use_gw || true
                if [[ ! "$_use_gw" =~ ^[Nn]$ ]]; then
                    GATEWAY="$DETECT_GW"
                    GW_SOURCE="本机探测"
                fi
            fi
        fi
    fi
fi

# ---- 1c. 交互补全: 只询问仍未提供的项 (按依赖顺序) ----
if [ "$INTERACTIVE" = true ]; then
    if [ -z "$NODE_NAME" ]; then
        read -r -p "请输入节点名称 (如 wk-edge-01): " NODE_NAME || true
    fi
    if [ -z "$NODE_IP" ]; then
        _hint="${DETECT_IP:-${NET_GATEWAY%.*}.101}"
        read -r -p "请输入静态IP (如 ${_hint}): " NODE_IP || true
        if [ -n "$NODE_IP" ]; then
            IP_SOURCE="交互输入"
        fi
    fi
    if [ -z "$HOSTNAME" ]; then
        read -r -p "请输入主机名 (如 edge-01): " HOSTNAME || true
    fi
    # DNS: 交互可覆盖清单/默认值 (回车 = 沿用候选值; 候选为自动获取则回车 = 自动获取)
    case "$DNS_SOURCE" in
        命令行|环境变量|交互输入) : ;;
        *)
            if [ "$DNS_MODE" = "dhcp" ]; then
                _dns_hint="DHCP 自动获取"
            else
                _dns_hint="$DNS_SERVERS"
            fi
            read -r -p "请输入 DNS (直接回车 = ${_dns_hint}; 多个用逗号分隔): " _dns_in || true
            if [ -n "$_dns_in" ]; then
                dns_apply_mode "$_dns_in"
                DNS_SOURCE="交互输入"
            fi
            ;;
    esac
fi

# ---- 1d. 默认值兜底 ----
if [ -z "$NODE_NAME" ]; then
    NODE_NAME="wk-node-01"
fi
if [ -z "$HOSTNAME" ]; then
    HOSTNAME="${NODE_NAME#wk-}"
fi
# DNS 默认值兜底: 无任何来源时使用"自动获取 (DHCP)", 不再写死 1.1.1.1
if [ "$DNS_MODE" != "dhcp" ] && [ -z "$DNS_SERVERS" ]; then
    DNS_MODE="dhcp"
fi

# IP 无法安全猜测: 缺失时明确报错, 而不是套用写死的网段
if [ -z "$NODE_IP" ]; then
    log_error "未指定节点 IP, 且清单中没有节点 '$NODE_NAME' 的记录, 也未能探测到本机 IP"
    echo ""
    echo "请任选一种方式:"
    echo "  1) 命令行指定:  $0 --node $NODE_NAME --ip <你的IP> --yes"
    echo "  2) 环境变量:    ONECLOUD_$(echo "$NODE_NAME" | tr '[:lower:]-.' '[:upper:]__')_IP=<你的IP> $0 --node $NODE_NAME --yes"
    echo "  3) 登记到清单:  在 inventory/nodes.yaml 或 nodes.local.yaml 中添加该节点"
    exit 1
fi

# ---- 1e. 网关与 IP 网段联动: 换了网段, 网关必须跟着变 ----
# 命令行显式给了 --gateway 时不推导 (尊重用户明确意图)
if [ "$GW_SOURCE" != "命令行" ]; then
    SUGGESTED_GW=""
    if NET_INFO="$(ip_net_info "$NODE_IP" "$LAN_PREFIX")"; then
        SUGGESTED_GW="${NET_INFO##* }"
    fi
    if [ -n "$SUGGESTED_GW" ]; then
        if [ -z "$GATEWAY" ]; then
            GATEWAY="$SUGGESTED_GW"
            GW_SOURCE="由IP推导"
        elif [ "$SUGGESTED_GW" != "$GATEWAY" ]; then
            # 判断现网关是否与 IP 同网段; 同网段则保留用户/清单的值
            CUR_GW_NET="$(ip_net_addr "$GATEWAY" "$LAN_PREFIX" 2>/dev/null || true)"
            IP_NET="$(ip_net_addr "$NODE_IP" "$LAN_PREFIX" 2>/dev/null || true)"
            if [ -n "$IP_NET" ] && [ "$CUR_GW_NET" != "$IP_NET" ]; then
                # --yes 直接应用; --dry-run 也要展示推导结果, 否则预览的是错误配置
                if [ "$ASSUME_YES" = true ] || [ "$DRY_RUN" = true ]; then
                    if [ "$DRY_RUN" = true ]; then
                        log_info "IP 网段已变更, 网关将自动由 ${GATEWAY} 调整为 ${SUGGESTED_GW}"
                    else
                        log_warn "IP 网段已变更, 网关自动由 ${GATEWAY} 调整为 ${SUGGESTED_GW}"
                    fi
                    GATEWAY="$SUGGESTED_GW"
                    GW_SOURCE="由IP推导"
                elif [ "$INTERACTIVE" = true ]; then
                    read -r -p "IP 已设为 ${NODE_IP}, 是否将网关由 ${GATEWAY} 改为 ${SUGGESTED_GW}? [Y/n] " _chg_gw || true
                    if [[ ! "$_chg_gw" =~ ^[Nn]$ ]]; then
                        GATEWAY="$SUGGESTED_GW"
                        GW_SOURCE="由IP推导"
                    fi
                else
                    log_warn "非交互且未指定 --yes: 网关仍为 ${GATEWAY} (与 IP ${NODE_IP} 不同网段, 建议显式指定 --gateway)"
                fi
            fi
        fi
    fi
fi

# 交互补全网关 (推导失败且未提供时才问)
if [ -z "$GATEWAY" ] && [ "$INTERACTIVE" = true ]; then
    read -r -p "请输入网关 (如 192.168.1.1): " GATEWAY || true
    if [ -n "$GATEWAY" ]; then GW_SOURCE="交互输入"; fi
fi

if [ -z "$GATEWAY" ]; then
    log_error "未指定网关 (--gateway 或 ONECLOUD_GATEWAY 或清单 network.gateway)"
    exit 1
fi
# 来源标注必须有值, 否则配置确认页会显示空来源
if [ -z "$IP_SOURCE" ]; then IP_SOURCE="默认"; fi
if [ -z "$GW_SOURCE" ]; then GW_SOURCE="默认"; fi

# ---- 1f. SD 卡决策: 无卡则跳过; 有卡则询问挂载/挂载点/自动挂载 ----
SD_ENABLE=false
SD_AUTOMOUNT=true
SD_ASKED=false

if [ "$NO_SD" = true ]; then
    log_info "已指定 --no-sd: 跳过 SD 卡挂载与 Docker 数据迁移"
elif [ -n "$SD_DEV" ]; then
    if [ -b "/dev/${SD_DEV}" ]; then
        SD_ENABLE=true
    else
        log_warn "--sd 指定的设备 /dev/${SD_DEV} 不存在"
    fi
fi

if [ "$NO_SD" != true ] && [ -n "$SD_DEV" ] && [ "$SD_ENABLE" = false ]; then
    log_warn "回退为自动探测 SD 卡"
    SD_DEV=""
fi

if [ "$NO_SD" != true ] && [ -z "$SD_DEV" ]; then
    detect_sd_cards
    case "${#SD_CANDIDATES[@]}" in
        0)
            log_warn "未检测到 SD 卡 / USB 存储: 跳过挂载与 Docker 数据迁移"
            ;;
        1)
            SD_DEV="${SD_CANDIDATES[0]}"
            SD_ENABLE=true
            log_info "检测到 SD 卡设备: /dev/${SD_DEV}"
            ;;
        *)
            if [ "$INTERACTIVE" = true ]; then
                echo "检测到多个可移动存储:"
                _i=1
                for _c in "${SD_CANDIDATES[@]}"; do
                    echo "  ${_i}) /dev/${_c}"
                    _i=$((_i + 1))
                done
                read -r -p "请选择要挂载的设备编号 (回车跳过): " _sdc || true
                case "$_sdc" in
                    ''|*[!0-9]*) log_info "未选择, 跳过 SD 卡挂载" ;;
                    *)
                        if [ "$_sdc" -ge 1 ] && [ "$_sdc" -le "${#SD_CANDIDATES[@]}" ]; then
                            SD_DEV="${SD_CANDIDATES[$((_sdc - 1))]}"
                            SD_ENABLE=true
                        else
                            log_warn "编号超出范围, 跳过 SD 卡挂载"
                        fi
                        ;;
                esac
            else
                SD_DEV="${SD_CANDIDATES[0]}"
                SD_ENABLE=true
                log_warn "检测到多个可移动存储, 默认使用 /dev/${SD_DEV}"
            fi
            ;;
    esac
fi

# 有卡: 询问是否挂载 / 挂载点 / 是否自动挂载
if [ "$SD_ENABLE" = true ]; then
    if [ "$INTERACTIVE" = true ]; then
        SD_ASKED=true
        read -r -p "是否挂载 SD 卡 /dev/${SD_DEV}? [Y/n] " _do_mount || true
        if [[ "$_do_mount" =~ ^[Nn]$ ]]; then
            SD_ENABLE=false
            log_info "已选择不挂载 SD 卡"
        else
            read -r -p "挂载点 [${SD_MOUNT}]: " _mp || true
            if [ -n "$_mp" ]; then SD_MOUNT="$_mp"; fi
            if [ "$NO_SD_AUTOMOUNT" = true ]; then
                SD_AUTOMOUNT=false
            else
                read -r -p "是否写入 /etc/fstab 开机自动挂载? [Y/n] " _auto || true
                if [[ "$_auto" =~ ^[Nn]$ ]]; then SD_AUTOMOUNT=false; fi
            fi
        fi
    fi
fi

if [ "$SD_ENABLE" = true ] && [ "$NO_SD_AUTOMOUNT" = true ]; then
    SD_AUTOMOUNT=false
fi

if [ "$SD_ENABLE" = true ] && [ "$SD_MOUNT" != "/mnt/sd" ]; then
    log_warn "挂载点 ${SD_MOUNT} 与集群脚本约定的 /mnt/sd 不一致"
    log_warn "      deploy.sh / backup.sh / setup.sh 等默认读写 /mnt/sd, 建议保持默认"
fi

# ---- 1g. 变更前安全检查: 网段 / IP 冲突 / 网关连通性 ----
NET_RISK=false
NET_RISK_REASONS=""
add_risk() {
    NET_RISK=true
    NET_RISK_REASONS="${NET_RISK_REASONS}  - $1
"
}

if [ -n "$DETECT_IP" ] || [ -n "$DETECT_GW" ]; then
    # 1) 新 IP 与本机当前 IP 是否同网段
    if [ -n "$DETECT_IP" ] && [ "$DETECT_IP" != "$NODE_IP" ]; then
        _cur_net="$(ip_net_addr "$DETECT_IP" "$LAN_PREFIX" 2>/dev/null || true)"
        _new_net="$(ip_net_addr "$NODE_IP" "$LAN_PREFIX" 2>/dev/null || true)"
        if [ -n "$_cur_net" ] && [ -n "$_new_net" ] && [ "$_cur_net" != "$_new_net" ]; then
            add_risk "新 IP ${NODE_IP}/${LAN_PREFIX} 与本机当前 IP ${DETECT_IP} 不在同一网段 (${_new_net} vs ${_cur_net})"
        fi
    fi
    # 2) 目标 IP 是否已被占用 (换 IP 时才检测, 避免 ping 到自己)
    if [ "$NODE_IP" != "$DETECT_IP" ] && ping_ok "$NODE_IP"; then
        add_risk "IP ${NODE_IP} 已被占用 (ping 有响应), 继续配置会造成 IP 冲突"
    fi
    # 3) 网关是否可达
    if [ -n "$GATEWAY" ] && ! ping_ok "$GATEWAY"; then
        add_risk "网关 ${GATEWAY} 当前 ping 不可达"
    fi
fi

# 4) 正在通过 SSH 操作, 同时又要改 IP: 这条连接必然当场断开
#    (这是"配完就失联"最常见的形态, 比 IP 冲突更容易发生)
if [ "$HAVE_NET_AUDIT" = true ] && net_audit_is_ssh \
   && [ -n "$DETECT_IP" ] && [ "$DETECT_IP" != "$NODE_IP" ]; then
    _ssh_from="$(net_audit_ssh_client | awk '{print $1}')"
    add_risk "当前是 SSH 会话 (来自 ${_ssh_from:-未知}), 把 IP 由 ${DETECT_IP} 改成 ${NODE_IP} 会立即断开这条连接"
fi
# 5) 防火墙现状本身就挡着 SSH (自检已在上方报告过, 这里再计入风险计数)
if [ "$NET_AUDIT_RISK" = true ] && [ "$HAVE_NET_AUDIT" = true ] \
   && ! net_audit_ssh_allowed; then
    add_risk "现有防火墙规则未放行 SSH 端口, 改完网络配置后可能无法重新登录"
fi

# ---- 1h. 最终一致性检查: 网关与 IP 不同网段时明确告警 ----
FINAL_IP_NET="$(ip_net_addr "$NODE_IP" "$LAN_PREFIX" 2>/dev/null || true)"
FINAL_GW_NET="$(ip_net_addr "$GATEWAY" "$LAN_PREFIX" 2>/dev/null || true)"
if [ -n "$FINAL_IP_NET" ] && [ -n "$FINAL_GW_NET" ] && [ "$FINAL_IP_NET" != "$FINAL_GW_NET" ]; then
    add_risk "网关 ${GATEWAY} 与 IP ${NODE_IP} 不在同一网段 (/${LAN_PREFIX})"
fi

if [ "$NET_RISK" = true ]; then
    echo ""
    log_warn "网络变更风险提示 (配置静态 IP 后可能无法联网):"
    printf '%s' "$NET_RISK_REASONS"
    log_warn "如确为跨网段迁移, 请确认目标网段有对应网关/路由后再执行"
    echo ""
fi

# DNS 自动获取的注意事项 (默认模式)
if [ "$DNS_MODE" = "dhcp" ] && [ "$DRY_RUN" != true ]; then
    log_warn "DNS 采用自动获取: 不向系统写入任何 nameserver, 交由 DHCP/系统自行提供"
    log_warn "      本脚本配置的是静态 IP, 若该机已无 DHCP 客户端在跑, 解析可能失败;"
    log_warn "      遇到解析异常请重新执行并指定: --dns <你的DNS>"
    echo ""
fi

# DNS 支持逗号分隔多个; 自动获取模式下不写入任何地址
DNS_LIST=""
if [ "$DNS_MODE" = "static" ]; then
    IFS=',' read -ra _dns_arr <<< "$DNS_SERVERS"
    for d in "${_dns_arr[@]}"; do
        d="$(echo "$d" | tr -d ' ')"
        [ -n "$d" ] && DNS_LIST="${DNS_LIST:+$DNS_LIST, }$d"
    done
else
    DNS_LIST="自动获取 (DHCP)"
fi

echo ""
log_info "配置信息:"
echo "  节点名称: $NODE_NAME"
echo "  静态IP:   $NODE_IP/${LAN_PREFIX}    (来源: ${IP_SOURCE})"
echo "  主机名:   $HOSTNAME"
echo "  网关:     $GATEWAY    (来源: ${GW_SOURCE})"
echo "  DNS:      ${DNS_LIST}    (来源: ${DNS_SOURCE})"
if [ "$SD_ENABLE" = true ]; then
    echo "  SD设备:   /dev/${SD_DEV}"
    echo "  挂载点:   ${SD_MOUNT}    (自动挂载: $([ "$SD_AUTOMOUNT" = true ] && echo 是 || echo 否))"
else
    echo "  SD设备:   未启用 (跳过挂载与 Docker 数据迁移)"
fi
if [ -n "$DETECT_IP" ] || [ -n "$DETECT_GW" ]; then
    echo "  本机现状: IP=${DETECT_IP:-未获取} 网关=${DETECT_GW:-未获取}"
fi
echo "  apt 动作:"
apt_switch_desc
echo ""

# 干跑: 只展示将要写入的配置, 不触碰系统 (放在确认之前, 可单独使用)
if [ "$DRY_RUN" = true ]; then
    log_info "干跑模式: 以上为将要应用的配置, 未做任何修改"
    echo ""
    echo "  将写入: /etc/network/interfaces 或 /etc/netplan/99-static.yaml"
    echo "  将设置: hostname=${HOSTNAME}, address=${NODE_IP}/${LAN_PREFIX}, gateway=${GATEWAY}"
    if [ "$DNS_MODE" = "dhcp" ]; then
        echo "  将设置: DNS 不写入 (自动获取, 由 DHCP/系统提供)"
    else
        echo "  将设置: DNS = ${DNS_LIST}"
    fi
    if [ "$SD_ENABLE" = true ]; then
        if [ "$SD_AUTOMOUNT" = true ]; then
            echo "  将挂载: /dev/${SD_DEV} -> ${SD_MOUNT} (并写入 /etc/fstab 自动挂载)"
        else
            echo "  将挂载: /dev/${SD_DEV} -> ${SD_MOUNT} (不写入 fstab)"
        fi
        echo "  将迁移: Docker 数据目录 -> ${SD_MOUNT}/docker"
    else
        echo "  将跳过: SD 卡挂载与 Docker 数据迁移"
    fi
    if [ "$DO_APT_PKGS" = true ]; then
        pkg_install_list
        echo "  将执行: 安装基础工具 (${INSTALL_PKGS})"
        if [ "${DO_EXTRA_PKGS:-false}" = true ]; then
            echo "  将执行: 安装可选工具 (${INSTALL_EXTRA_PKGS:-无 —— 全部被黑名单剔除})"
        else
            echo "  将跳过: 可选工具 (默认不装, 需要时加 --extra-pkgs)"
        fi
    else
        echo "  将跳过: 基础工具与可选工具安装"
    fi
    if apt_switch_all_off; then
        echo "  将跳过: 全部 apt 动作 (换源/刷新索引/升级/装包) —— 一个字节都不动 apt"
    fi
    echo ""
    exit 0
fi

if [ "$ASSUME_YES" = true ]; then
    CONFIRM="y"
    if [ "$NET_RISK" = true ]; then
        log_warn "--yes 已指定: 存在网络风险但继续执行 (请自行确认不会失联)"
    fi
elif [ "$INTERACTIVE" = true ]; then
    if [ "$NET_RISK" = true ]; then
        read -r -p "存在网络风险, 确认继续? 输入 yes 继续, 其他任意键取消: " CONFIRM || true
        [[ "$CONFIRM" = "yes" ]] || { log_warn "已取消"; exit 0; }
        CONFIRM="y"
    else
        read -r -p "确认无误? [y/N] " CONFIRM || true
    fi
else
    log_warn "非交互环境且未指定 --yes, 已取消"
    exit 0
fi
[[ "$CONFIRM" =~ ^[Yy]$ ]] || { log_warn "已取消"; exit 0; }

# ---- 2. 设置主机名 ----
log_info "设置主机名: $HOSTNAME"
hostnamectl set-hostname "$HOSTNAME"
echo "$HOSTNAME" > /etc/hostname

# ---- 3. 换国内源 (可选, 默认跳过) ----
APT_MIRROR="${ONECLOUD_APT_MIRROR:-https://mirrors.tuna.tsinghua.edu.cn/debian}"
APT_SEC_MIRROR="${ONECLOUD_APT_SECURITY_MIRROR:-https://mirrors.tuna.tsinghua.edu.cn/debian-security}"
APT_SOURCES_FILE=""

_distro_info="$(detect_distro)"
DISTRO_ID="${_distro_info%% *}"
DISTRO_CODENAME="${_distro_info#* }"
APT_CODENAME="${ONECLOUD_DEBIAN_CODENAME:-${DISTRO_CODENAME:-bullseye}}"
log_info "系统: ID=${DISTRO_ID} 代号=${DISTRO_CODENAME:-未知}"
APT_SRC_ETC="${ONECLOUD_ETC_ROOT:-/etc}"

if [ "$DO_MIRROR" != true ]; then
    log_info "换源: 跳过 (默认; 需要换源请加 --mirror)"
    log_info "      沿用系统现有源: ${APT_SRC_ETC}/apt/sources.list 或 sources.list.d/debian.sources"
elif [ "$DISTRO_ID" = "debian" ] || [ "$DISTRO_ID" = "armbian" ] || [ "$DISTRO_ID" = "unknown" ]; then
    log_info "配置国内 apt 源 (镜像: ${APT_MIRROR}, 代号 ${APT_CODENAME})"
    configure_apt_sources "$APT_CODENAME" "$APT_MIRROR" "$APT_SEC_MIRROR"
else
    log_warn "系统 ID=${DISTRO_ID} 非 Debian 系, 跳过换源 (保留系统原有源)"
fi

# ---- 4. 更新系统 (可选, 默认跳过) ----
if [ "$DO_APT_UPDATE" != true ] && [ "$DO_APT_UPGRADE" != true ]; then
    log_info "更新: 跳过 (默认; 需要时加 --apt-update 刷索引 / --apt-upgrade 升级系统包)"
else
    if [ "$DO_APT_UPDATE" = true ]; then
        if [ "$APT_UPDATE_AUTO" = true ]; then
            log_warn "换源后自动补一步刷新索引 (不想刷请加 --no-apt-update)"
        fi
        # 刷新索引失败不致命: 后面装包会连带失败, 届时给出明确指引
        apt_try "刷新软件包索引 (apt update)" apt update
    else
        log_info "刷新索引: 跳过 (未指定 --apt-update)"
    fi
    if [ "$DO_APT_UPGRADE" = true ]; then
        # 升级允许失败: 玩客云常因内核/firmware 升级需重启而中断,
        # 不该因此让整机初始化停在一半
        apt_try "升级已安装软件包 (apt upgrade)" apt upgrade -y
    else
        log_info "升级系统包: 跳过 (未指定 --apt-upgrade)"
    fi
fi

# ---- 5. 安装基础工具 (默认只装核心包; 可选包需 --extra-pkgs 显式开启) ----
# 注意: 这里刻意不含 wireguard-dkms —— Debian 12 (bookworm) 起该包已从仓库移除
#       (bullseye 尚在; 内核 5.6+ 已内置 wireguard 模块, 本就无需 dkms)。
#       老内核且源里确实提供该包时才按需安装, 见下方。
# 目标机是无头服务器: 任何桌面套件 / Xorg / 显示管理器 / GUI 应用都不在清单里,
# 即便用户经 --extra-pkgs 显式列出, 也会被 pkg_gui_filter 剔除。
if [ "$DO_APT_PKGS" != true ]; then
    log_info "跳过基础工具安装 (--no-apt-pkgs): 请自行确认 ${BASE_PKGS} 已就绪"
else
    pkg_install_list
    if [ "${DO_EXTRA_PKGS:-false}" != true ]; then
        log_info "可选工具默认不装 (wget/vim/htop/iotop/net-tools/...); 需要请加 --extra-pkgs"
    fi
    log_info "安装基础工具: ${INSTALL_PKGS}"
    if [ -z "$INSTALL_PKGS" ]; then
        log_warn "装包清单为空, 跳过 apt install"
    elif ! apt_run "安装基础工具" apt install -y $INSTALL_PKGS; then
        log_warn "基础工具安装失败, 已继续后续步骤 (不中断初始化)"
        if [ "$DO_APT_UPDATE" != true ]; then
            log_warn "  当前未刷新 apt 索引, 多半是索引过期; 可加 --apt-update 重跑, 或手工 apt update"
        else
            log_warn "  索引已刷新仍失败, 请检查源可达性与磁盘空间: df -h /"
        fi
    fi

    if wireguard_kernel_builtin; then
        log_info "内核 $(uname -r) 已内置 wireguard 模块, 无需 wireguard-dkms"
    elif apt-cache show wireguard-dkms >/dev/null 2>&1; then
        log_warn "内核 $(uname -r) 未见内置 wireguard, 源中提供 wireguard-dkms, 安装之"
        apt_run "安装 wireguard-dkms" apt install -y wireguard-dkms
    else
        log_warn "内核 $(uname -r) 较老且当前源不提供 wireguard-dkms, 已跳过"
        log_warn "  如需 wireguard, 请先确认模块可用: modinfo wireguard"
    fi
fi

# ---- 6. 配置时区 ----
log_info "设置时区: Asia/Shanghai"
ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime
echo "Asia/Shanghai" > /etc/timezone

# ---- 7. 创建 swapfile ----
if [ ! -f /swapfile ]; then
    log_info "创建 2GB swapfile..."
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
    echo "vm.swappiness=10" >> /etc/sysctl.conf
    sysctl vm.swappiness=10
fi

# ---- 8. SD 卡分区和挂载 (无卡/未选择挂载则跳过) ----
SD_MOUNTED=false
if [ "$SD_ENABLE" != true ]; then
    log_info "跳过 SD 卡挂载 (未检测到 SD 卡或已选择跳过)"
else
    SD_PATH="/dev/${SD_DEV}"
    if [ ! -b "$SD_PATH" ]; then
        log_error "未找到 SD 卡设备 ${SD_PATH}, 跳过挂载"
    else
        SD_PART="${SD_PATH}p1"
        # 无分区表时, 可能是整卡直挂
        [ -b "$SD_PART" ] || SD_PART="$SD_PATH"
        log_info "挂载 SD 卡 ${SD_PART} -> ${SD_MOUNT}..."
        mkdir -p "$SD_MOUNT"
        if mountpoint -q "$SD_MOUNT"; then
            SD_MOUNTED=true
            log_info "${SD_MOUNT} 已挂载, 复用"
        elif mount "$SD_PART" "$SD_MOUNT" 2>/dev/null; then
            SD_MOUNTED=true
        else
            log_warn "SD 卡可能未格式化, 尝试创建分区并格式化..."
            parted -s "$SD_PATH" mklabel gpt mkpart primary ext4 1MiB 100%
            sleep 2
            SD_PART="${SD_PATH}p1"
            [ -b "$SD_PART" ] || SD_PART="$SD_PATH"
            if mkfs.ext4 -F "$SD_PART" && mount "$SD_PART" "$SD_MOUNT"; then
                SD_MOUNTED=true
            else
                log_error "SD 卡挂载失败, 跳过 (Docker 数据将保留在默认位置)"
            fi
        fi
        # 自动挂载: 幂等写入 fstab
        if [ "$SD_MOUNTED" = true ] && [ "$SD_AUTOMOUNT" = true ]; then
            if ! grep -qE "[[:space:]]${SD_MOUNT}[[:space:]]" /etc/fstab 2>/dev/null; then
                echo "${SD_PART} ${SD_MOUNT} ext4 defaults,noatime 0 2" >> /etc/fstab
                log_info "已写入 /etc/fstab, 开机自动挂载"
            fi
        elif [ "$SD_MOUNTED" = true ]; then
            log_info "未启用自动挂载 (本次挂载在重启后失效)"
        fi
    fi
fi

# ---- 9. 迁移 Docker 数据 (仅在 SD 卡挂载成功时) ----
if [ "$SD_MOUNTED" = true ]; then
    log_info "迁移 Docker 数据到 ${SD_MOUNT}..."
    mkdir -p "${SD_MOUNT}/docker" "${SD_MOUNT}/srv" "${SD_MOUNT}/backups"

    if ! grep -q "DOCKER_OPTS" /etc/default/docker 2>/dev/null; then
        if [ -d /var/lib/docker ] && [ "$(ls -A /var/lib/docker 2>/dev/null)" ]; then
            systemctl stop docker 2>/dev/null || true
            rsync -avhP /var/lib/docker/ "${SD_MOUNT}/docker/" || true
        fi
        echo "DOCKER_OPTS=\"-g ${SD_MOUNT}/docker --log-driver=json-file --log-opt max-size=5m --log-opt max-file=2\"" >> /etc/default/docker
        mkdir -p /etc/docker
        cat > /etc/docker/daemon.json << EOF
{
  "data-root": "${SD_MOUNT}/docker",
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "5m",
    "max-file": "2"
  },
  "registry-mirrors": [
    "https://dockerproxy.com",
    "https://mirror.baidubce.com"
  ]
}
EOF
        systemctl start docker
        log_info "Docker 已迁移到 ${SD_MOUNT}/docker"
    fi
else
    log_warn "SD 卡未挂载: 保留 Docker 默认数据目录 /var/lib/docker"
    log_warn "      集群脚本默认读写 /mnt/sd, 若未插卡请确认 eMMC 空间充足"
fi

# ---- 10. 固定 Docker 版本 ----
if dpkg -l | grep -q docker.io; then
    log_info "锁定 Docker 版本 (防止升级到 v29+)..."
    apt-mark hold docker.io
fi

# ---- 11. 设置静态 IP ----
log_info "配置静态 IP: $NODE_IP/${LAN_PREFIX} (网关 $GATEWAY)"
# ONECLOUD_ETC_ROOT 仅用于测试/演练时把 /etc 指到别处 (默认 /etc)
NET_ETC="${ONECLOUD_ETC_ROOT:-/etc}"

# 断链防护 (只备份 + 告知回滚, 不自动回滚):
#   改写 interfaces / netplan 并生效的瞬间, 旧地址就没了。如果这条命令是通过
#   SSH 发的, 连接当场断; 万一新配置有问题, 人也回不来了。
#   先把 network/ netplan/ 整体打个快照, 并把"怎么退回去"打在屏幕上。
NET_BACKUP_TAR=""
if [ "$DRY_RUN" != true ]; then
    _net_ts="$(date +%Y%m%d-%H%M%S)"
    _net_backup_dir="${NET_ETC}/onecloud/net-backup-${_net_ts}"
    if mkdir -p "$_net_backup_dir" 2>/dev/null \
       && tar -C "$NET_ETC" -cf "${_net_backup_dir}/net-config.tar" \
              network netplan 2>/dev/null; then
        NET_BACKUP_TAR="${_net_backup_dir}/net-config.tar"
        log_info "原网络配置已备份: ${NET_BACKUP_TAR}"
    else
        log_warn "未能备份原网络配置 (目录不存在或无权限), 继续配置"
    fi
fi

# SSH 远端改 IP: 明确告知"会断", 并给出重新登录与回滚的动作
if [ "$HAVE_NET_AUDIT" = true ] && net_audit_is_ssh 2>/dev/null \
   && [ -n "$DETECT_IP" ] && [ "$DETECT_IP" != "$NODE_IP" ]; then
    echo ""
    log_warn "当前是 SSH 会话, 新地址生效的瞬间这条连接就会断开 —— 属正常现象"
    log_warn "  请改用新地址重新登录: ssh <用户>@${NODE_IP}"
    if [ -n "$NET_BACKUP_TAR" ]; then
        log_warn "  若新地址连不上, 到节点本地控制台回滚:"
        log_warn "    tar -C ${NET_ETC} -xf ${NET_BACKUP_TAR} && netplan apply"
    fi
    echo ""
fi

if [ -f "${NET_ETC}/network/interfaces" ]; then
    if [ "$DNS_MODE" = "dhcp" ]; then
        DNS_LINE="# DNS 不写死: 由 DHCP/系统提供 (bootstrap --dns dhcp)"
        log_info "DNS: 自动获取 (不向 interfaces 写入 dns-nameservers)"
    else
        DNS_LINE="    dns-nameservers ${DNS_LIST}"
        log_info "DNS: ${DNS_LIST}"
    fi
    cat > "${NET_ETC}/network/interfaces" << EOF
auto lo
iface lo inet loopback

auto eth0
iface eth0 inet static
    address ${NODE_IP}/${LAN_PREFIX}
    gateway ${GATEWAY}
${DNS_LINE}
EOF
elif [ -d "${NET_ETC}/netplan" ]; then
    if [ "$DNS_MODE" = "dhcp" ]; then
        # 静态地址 + dhcp4 仅取 DNS: use-routes/use-ntp 关掉, 免得 DHCP 抢默认路由
        log_info "DNS: 自动获取 (netplan dhcp4 只取 DNS)"
        {
            cat << EOF
network:
  version: 2
  ethernets:
    eth0:
      dhcp4: true
      dhcp4-overrides:
        use-routes: false
        use-ntp: false
      addresses:
        - ${NODE_IP}/${LAN_PREFIX}
      routes:
        - to: default
          via: ${GATEWAY}
EOF
        } > "${NET_ETC}/netplan/99-static.yaml"
    else
        # netplan 的 nameservers 需要 YAML 列表形式
        DNS_YAML=""
        IFS=',' read -ra _dns_arr2 <<< "$DNS_SERVERS"
        for d in "${_dns_arr2[@]}"; do
            d="$(echo "$d" | tr -d ' ')"
            [ -n "$d" ] && DNS_YAML="${DNS_YAML}        - ${d}
"
        done
        log_info "DNS: ${DNS_LIST}"
        # netplan 的 nameservers 需要 YAML 列表形式; DNS 行数不定, 故分两段输出
        # (heredoc 结束符必须独占一行, 不能被变量展开吞掉)
        {
            cat << EOF
network:
  version: 2
  ethernets:
    eth0:
      addresses:
        - ${NODE_IP}/${LAN_PREFIX}
      routes:
        - to: default
          via: ${GATEWAY}
      nameservers:
        addresses:
EOF
            printf '%s' "$DNS_YAML"
        } > "${NET_ETC}/netplan/99-static.yaml"
    fi
    # 先校验再生效: netplan generate 只生成后端配置, 不影响运行态。
    # 校验不过就不 apply —— 让旧配置继续顶着, 总好过把机器留在半截状态。
    if command -v netplan >/dev/null 2>&1; then
        if netplan generate >/dev/null 2>&1; then
            netplan apply 2>/dev/null || true
        else
            log_error "netplan 配置校验未通过, 本次不执行 apply (旧配置仍在生效)"
            if [ -n "$NET_BACKUP_TAR" ]; then
                log_error "  回滚: tar -C ${NET_ETC} -xf ${NET_BACKUP_TAR}"
            fi
        fi
    fi
fi

# ---- 12. 配置 /etc/hosts (幂等: 避免重复追加) ----
log_info "配置 hosts..."
# 条目来源: inventory 清单 (若可用); 否则至少有本机自己
HOST_ENTRIES=""
if [ "$HAVE_INVENTORY" = true ]; then
    for N in "${ALL_NODES[@]}"; do
        IFS='|' read -r N_NAME N_HOST N_IP N_WG _r <<< "$N"
        H_FQDN="${N_HOST}.lan"
        HOST_ENTRIES="${HOST_ENTRIES}${N_IP}  ${N_NAME} ${H_FQDN}
"
        [ -n "$N_WG" ] && HOST_ENTRIES="${HOST_ENTRIES}${N_WG}  ${N_NAME}.wg
"
    done
fi
# 保证本机一定在 hosts 中
case "$HOST_ENTRIES" in
    *"${NODE_NAME} "*) : ;;
    *) HOST_ENTRIES="${HOST_ENTRIES}${NODE_IP}  ${NODE_NAME} ${HOSTNAME}.lan
" ;;
esac

while read -r host_ip host_alias; do
    [ -z "$host_ip" ] && continue
    for alias in $host_alias; do
        if ! grep -qE "[[:space:]]${alias}([[:space:]]|$)" /etc/hosts 2>/dev/null; then
            echo "${host_ip}  ${alias}" >> /etc/hosts
        fi
    done
done <<< "$HOST_ENTRIES"

# ---- 13. 启用 IP 转发 ----
log_info "启用 IP 转发..."
grep -q '^net.ipv4.ip_forward=1' /etc/sysctl.conf 2>/dev/null || \
    echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf
grep -q '^net.ipv4.conf.all.src_valid_mark=1' /etc/sysctl.conf 2>/dev/null || \
    echo 'net.ipv4.conf.all.src_valid_mark=1' >> /etc/sysctl.conf
sysctl -p 2>/dev/null || true

# ---- 14. 创建目录结构 (安装路径自适应: SD 可用用 SD, 否则回退 /opt) ----
# --no-sd 时用户显式跳过 SD 卡, 直接走 /opt 回退; 否则运行时重新评估 SD 真实状态
if [ "$SD_ENABLE" != true ]; then
    DATA_ROOT="${INSTALL_FALLBACK_ROOT:-/opt/onecloud}"
    INSTALL_VIA_SD=0
    INSTALL_SOURCE="本地回退 (/opt) - 用户指定 --no-sd"
    log_warn "已指定 --no-sd: 安装目录固定为 ${DATA_ROOT}"
else
    resolve_data_root
fi

# 服务数据目录清单 (相对 srv/<节点>); SD 写入失败会自动降级到 /opt 同路径
SVC_TREE="cloudflared adguard/{work,conf} wireguard/config \
    clash memos/data homeassistant piwigo/{config,gallery} xiaomusic \
    migpt syncthing/{config,data} verysync/{temp} aria2/{config,downloads} \
    cupsd/{config,printers,spool} cups-web/config panel"

log_info "创建目录结构: ${DATA_ROOT}/srv/${NODE_NAME} (来源: ${INSTALL_SOURCE})"
safe_install_tree "srv/${NODE_NAME}" "${SVC_TREE}" "服务数据目录"

# ---- 15. 生成 SSH 密钥 (如不存在) ----
if [ ! -f /root/.ssh/id_ed25519 ]; then
    log_info "生成 SSH 密钥..."
    ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519 -C "root@${HOSTNAME}"
    chmod 600 /root/.ssh/id_ed25519
fi

# ---- 15b. 记录安装信息 + 回写节点清单 (供面板/部署/备份同步) ----
# ① 节点侧权威记录: 数据根目录与节点身份。
#    控制端 deploy/backup/restore 与面板配置同步据此定位远程路径,
#    避免「无 SD 卡回退 /opt」的节点仍被按 /mnt/sd 读写。
WG_IP_EFF="$(node_wg_ip "$NODE_NAME" 2>/dev/null || true)"
INSTALL_CONF_DIR="${ONECLOUD_ETC_ROOT:-/etc}/onecloud"
if mkdir -p "$INSTALL_CONF_DIR" 2>/dev/null; then
    {
        echo "# 由 scripts/bootstrap.sh 生成: 节点安装事实 (勿手改)"
        echo "NODE_NAME=${NODE_NAME}"
        echo "HOSTNAME=${HOSTNAME}"
        echo "NODE_IP=${NODE_IP}"
        echo "WG_IP=${WG_IP_EFF}"
        echo "DATA_ROOT=${DATA_ROOT}"
        echo "INSTALL_VIA_SD=${INSTALL_VIA_SD}"
    } > "${INSTALL_CONF_DIR}/install.conf" 2>/dev/null \
        && log_info "已记录安装信息: ${INSTALL_CONF_DIR}/install.conf" \
        || log_warn "写入 ${INSTALL_CONF_DIR}/install.conf 失败"
fi

# ② 把本次选定的 IP/主机名 回写到 inventory/nodes.local.yaml。
#    否则「部署时改的 IP」只落在本机网络配置里, 面板/部署脚本读到的仍是旧值,
#    面板就会因 IP 不符而无法监控/操作该节点。
if [ -d "${SCRIPT_DIR%/scripts}/inventory" ]; then
    if update_local_inventory "$NODE_NAME" "$NODE_IP" "$HOSTNAME" "$WG_IP_EFF"; then
        log_info "已回写节点清单: inventory/nodes.local.yaml (面板配置将据此刷新)"
    else
        log_warn "回写 inventory/nodes.local.yaml 失败 (可手工登记该节点)"
    fi
else
    log_info "未找到 inventory/ 目录, 跳过清单回写; 请在控制端手工登记:"
    echo "          - name: ${NODE_NAME}"
    echo "            hostname: ${HOSTNAME}"
    echo "            ip: ${NODE_IP}"
fi

# ---- 16. 显示完成信息 ----
echo ""
log_info "=========================================="
log_info "  初始化完成!"
log_info "=========================================="
echo ""
echo "节点信息:"
echo "  主机名: $HOSTNAME"
echo "  IP:     $NODE_IP"
if [ "$INSTALL_VIA_SD" = 1 ]; then
    echo "  存储:   ${DATA_ROOT} (SD卡, 自动挂载: $([ "$SD_AUTOMOUNT" = true ] && echo 是 || echo 否))"
else
    echo "  存储:   回退安装目录 (${INSTALL_SOURCE})"
fi
echo "  Swap:   2GB"
echo ""
echo "下一步:"
echo "  1. 将此节点的 SSH 公钥添加到其他节点的 authorized_keys"
echo "  2. 克隆 onecloud-cluster 仓库到 ${DATA_ROOT}/"
echo "  3. 复制对应 node-xxx 目录的 docker-compose.yml 到 ${DATA_ROOT}/srv/${NODE_NAME}/"
echo "  4. 运行 ./scripts/deploy.sh 分发配置"
echo "  5. 启动服务: cd ${DATA_ROOT}/srv/${NODE_NAME} && docker-compose up -d"
echo ""
echo "SSH 公钥:"
cat /root/.ssh/id_ed25519.pub
echo ""
log_info "建议立即重启: reboot"
