#!/bin/bash
# ============================================================
# 快速安装脚本 - 单个服务 (install-services.sh)
# 在节点上运行, 安装指定服务的原生二进制或 Docker
# ============================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# Python 依赖安装公共库 (pip 缺失时的多路降级)
# shellcheck source=lib-pydeps.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib-pydeps.sh"

# 节点清单库 + 安装态/数据根单一真相库
# 修复: 本脚本原先既不 source lib-nodes.sh, 也从不定义 DATA_ROOT,
#       导致 ${DATA_ROOT:-/mnt/sd/srv} 恒定为 /mnt/sd/srv (见审计 C-3)。
#       现在数据根由 lib-services.sh 单点提供, 无 SD 回退 /opt/onecloud 也能正确取到。
# shellcheck source=lib-nodes.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib-nodes.sh"
# shellcheck source=lib-services.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib-services.sh"

# 本机数据根 (lib-install-path.sh 可用时优先按其决策, 否则回退默认值)
if [ -f "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib-install-path.sh" ]; then
    # shellcheck source=lib-install-path.sh
    source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib-install-path.sh"
    # 本机执行时按实际 SD 卡状态决策; 失败不阻断 (resolve_data_root 自身已容错)
    [ -z "${ONECLOUD_SKIP_DATA_ROOT_RESOLVE:-}" ] && resolve_data_root 2>/dev/null || true
fi
# 当前节点名 (供本机场景定位数据目录; 取不到时由调用方按角色传参)
CURRENT_NODE="$(node_by_ip "$(hostname -I 2>/dev/null | awk '{print $1}')" 2>/dev/null || true)"
[ -z "$CURRENT_NODE" ] && CURRENT_NODE="$(node_names | head -n 1)"

# 数据根目录 (集中一处, 供本脚本所有路径拼接使用)
svc_data_root() { oc_data_root; }

# ----------------------------------------------------------------------------
# 渲染模板里的 __DATA_ROOT__ / __NODE_NAME__ 占位符
#   配置文件 (xiaomusic config.json / verysync config.yaml) 与 systemd 单元
#   都只写占位符, 安装时才落到实际数据根。这样 SD 卡用户与无卡用户共用一份
#   受版本控制的配置, 不会有人误把 /mnt/sd 提交进去 (审计 C-3)。
#
#   用法: render_template <源文件> <目标文件>
#   源文件不存在 -> 返回 0 (跳过, 由调用方决定是否告警)
# ----------------------------------------------------------------------------
render_template() {
    local src="${1:-}" dst="${2:-}"
    [ -n "$src" ] && [ -n "$dst" ] || return 1
    [ -f "$src" ] || return 0
    local dr
    dr="$(svc_data_root)"
    mkdir -p "$(dirname "$dst")"
    sed -e "s|__DATA_ROOT__|${dr}|g" \
        -e "s|__NODE_NAME__|${CURRENT_NODE}|g" \
        "$src" > "$dst"
}

# 定位仓库内某节点的某服务配置模板 (脚本从仓库 scripts/ 运行时可用)
service_template_dir() {   # service_template_dir <节点名> <服务名>
    local n="${1:-}" s="${2:-}" root
    root="${LIB_NODES_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
    printf '%s/node-%s/%s' "$root" "$n" "$s"
}

usage() {
    cat << EOF
用法: $0 <服务名|子命令>

可安装的服务 (原生二进制):
  mihomo       - Clash Meta 代理
  xiaomusic    - 小米音乐
  migpt        - AI 助手 (proxy.py)
  verysync     - 微力同步 (无自动安装实现, 打印手动部署指引)
  all-native   - 安装所有原生服务

Docker 服务 (按角色启动对应节点):
  edge         - 边缘网关节点全部 Docker 服务
  iot          - IoT 节点全部 Docker 服务
  storage      - 存储与同步节点全部 Docker 服务
  all-docker   - 安装全部

状态查询:
  list-installed  列出各服务的安装态与组网模式

示例:
  $0 mihomo            # 安装 Clash
  $0 xiaomusic         # 安装 xiaomusic
  $0 edge              # 启动边缘网关节点所有容器
  $0 all-native        # 安装所有原生二进制
  $0 list-installed    # 查看安装态

说明:
  数据根由 lib-services.sh 统一解析 (SD 卡挂载点, 无卡回退 /opt/onecloud),
  不再固定使用 /mnt/sd。配置模板里的 __DATA_ROOT__ 占位符会在安装时按本机
  实际数据根渲染。未安装的服务可在面板上区分显示。
EOF
}

# 原生二进制安装需要写 /usr/local/bin, 必须 root
require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        log_error "需要 root 权限 (请用 sudo $0 <服务名> 运行)"
        return 1
    fi
    return 0
}

# 校验下载产物确实是 ELF 可执行文件, 防止把 404 页面/空文件当成安装成功
is_elf_binary() {
    local f=${1:-}
    [ -n "$f" ] && [ -s "$f" ] || return 1
    [ "$(head -c 4 "$f" 2>/dev/null | od -An -tx1 2>/dev/null | tr -d ' \n')" = "7f454c46" ]
}

install_mihomo() {
    log_info "安装 mihomo (Clash Meta)..."
    require_root || return 1
    local VER arch url tmpdir

    VER=$(curl -fsSL https://api.github.com/repos/MetaCubeX/mihomo/releases/latest \
          | grep '"tag_name"' | sed -E 's/.*"([^"]+)".*/\1/')
    if [ -z "$VER" ]; then
        log_error "获取版本号失败 (网络不通或 GitHub API 限流)"
        return 1
    fi

    case "$(uname -m)" in
        aarch64|arm64)        arch="arm64" ;;
        x86_64|amd64)         arch="amd64" ;;
        armv7l|armv6l|armhf)  arch="armv7" ;;
        *) log_error "不支持的架构: $(uname -m)"; return 1 ;;
    esac
    log_info "最新版本: $VER (${arch})"

    url="https://github.com/MetaCubeX/mihomo/releases/download/${VER}/mihomo-linux-${arch}-${VER}.gz"
    tmpdir=$(mktemp -d)
    if ! curl -fsSL "$url" -o "${tmpdir}/mihomo.gz"; then
        log_error "下载失败: $url"
        rm -rf "$tmpdir"; return 1
    fi
    if ! gunzip -c "${tmpdir}/mihomo.gz" > "${tmpdir}/mihomo" 2>/dev/null; then
        log_error "解压失败, 下载内容不是有效的 gzip 包"
        rm -rf "$tmpdir"; return 1
    fi
    if ! is_elf_binary "${tmpdir}/mihomo"; then
        log_error "下载产物不是可执行文件 (可能是 404 页面), 已中止"
        rm -rf "$tmpdir"; return 1
    fi

    mkdir -p /usr/local/bin
    install -m 0755 "${tmpdir}/mihomo" /usr/local/bin/mihomo
    rm -rf "$tmpdir"

    if command -v mihomo >/dev/null 2>&1; then
        log_info "mihomo 已安装: $(mihomo -v 2>&1 | head -1)"
    else
        log_error "安装后未在 PATH 中找到 mihomo"
        return 1
    fi

    # 数据目录按本机实际数据根创建 (不再写死 /mnt/sd)
    local clash_dir
    clash_dir="$(service_data_dir "$CURRENT_NODE" clash)"
    mkdir -p "$clash_dir"
    log_info "数据目录: ${clash_dir}"
    log_info "systemd 单元: 运行 node-${CURRENT_NODE}/clash/install-service.sh"
}

install_xiaomusic() {
    log_info "安装 xiaomusic..."
    require_root || return 1
    local ver
    ver=$(curl -sL https://api.github.com/repos/hanxi/xiaomusic/releases/latest | grep '"tag_name"' | sed -E 's/.*"([^"]+)".*/\1/')
    if [ -z "$ver" ]; then
        log_error "获取版本失败"
        return 1
    fi
    log_info "最新版本: $ver"

    local arch="armv7"
    case "$(uname -m)" in
        aarch64|arm64) arch="arm64" ;;
        x86_64)        arch="amd64" ;;
    esac

    local url
    url=$(curl -sL https://api.github.com/repos/hanxi/xiaomusic/releases/latest \
        | grep "browser_download_url.*linux.*${arch}" | head -1 \
        | sed -E 's/.*"([^"]+)".*/\1/')

    if [ -z "$url" ]; then
        log_error "未找到 ${arch} 版本, 请手动下载: https://github.com/hanxi/xiaomusic/releases"
        return 1
    fi

    log_info "下载: $url"
    local tmpdir
    tmpdir=$(mktemp -d)
    curl -sL "$url" -o "$tmpdir/xiaomusic.tar.gz"
    tar xzf "$tmpdir/xiaomusic.tar.gz" -C "$tmpdir"
    if [ -f "$tmpdir/xiaomusic" ] && is_elf_binary "$tmpdir/xiaomusic"; then
        mkdir -p /usr/local/bin
        install -m 0755 "$tmpdir/xiaomusic" /usr/local/bin/xiaomusic
        log_info "xiaomusic 已安装"
    else
        log_error "下载包结构异常或不是可执行文件, 请手动安装"
        rm -rf "$tmpdir"
        return 1
    fi
    rm -rf "$tmpdir"

    # 渲染配置模板到本机实际数据根 (路径不再写死 /mnt/sd)
    local xm_dir tpl
    xm_dir="$(service_data_dir "$CURRENT_NODE" xiaomusic)"
    tpl="$(service_template_dir "$CURRENT_NODE" xiaomusic)/config.json"
    mkdir -p "${xm_dir}/downloads" "${xm_dir}/cache" "${xm_dir}/session"
    if [ -f "$tpl" ]; then
        render_template "$tpl" "${xm_dir}/config.json"
        log_info "配置已渲染: ${xm_dir}/config.json"
    else
        log_warn "未找到配置模板 ($tpl), 请手动创建 ${xm_dir}/config.json"
    fi
    log_info "systemd 单元: 运行 node-${CURRENT_NODE}/xiaomusic/install-service.sh"
}

install_migpt() {
    log_info "安装 migpt 轻量代理依赖..."
    local pkgs="flask flask-cors pyyaml requests"

    # 统一走 scripts/lib-pydeps.sh 的降级链:
    #   pip -> --break-system-packages -> ensurepip/apt 补 python3-pip
    #   -> apt 发行版包 (python3-flask 等) -> import 校验
    # 旧实现直接用裸 pip3, 在「装了 python3 但没装 python3-pip」的机器上
    # 会以 command not found 起步, 且 --break-system-packages 救不了。
    local py
    py="$(pydeps_pick_python)" || {
        log_error "未检测到 python3, 请先安装: apt-get install -y python3 python3-pip"
        return 1
    }
    if ! pydeps_install "$py" "$pkgs" ""; then
        log_error "依赖安装失败, 可手工执行:"
        pydeps_hint "$pkgs" "$py" "" >&2
        return 1
    fi
    log_info "migpt proxy.py 已就绪, 使用 systemd 运行"
}

install_verysync() {
    # verysync 在 services.yaml 里标了 install: manual —— 无自动化安装实现。
    # 这里如实说明并提供下载与部署指引, 不再只是"需要手动下载"一句话。
    local dir
    dir="$(service_data_dir "$CURRENT_NODE" verysync)"
    log_warn "verysync 无自动化安装实现 (services.yaml 标记 install: manual)"
    echo ""
    echo "  手动安装步骤:"
    echo "    1. 从 https://www.verysync.com/download 下载 Linux ARM 版本"
    echo "       (玩客云 S805 为 armv7, 选择 armv7 / arm 版)"
    echo "    2. 放到节点并安装:"
    echo "         tar -xzf verysync-linux-armv7-*.tar.gz"
    echo "         install -m 0755 verysync /usr/local/bin/verysync"
    echo "    3. 数据目录 (已按本机数据根解析):"
    echo "         mkdir -p '${dir}'"
    echo "    4. 写入 systemd 单元后启用:"
    echo "         ExecStart=/usr/local/bin/verysync -gui-address=0.0.0.0:19900 -config=${dir}"
    echo "         systemctl daemon-reload && systemctl enable --now verysync"
    echo ""
    # 配置模板里的路径占位符按本机数据根渲染, 避免手工再改一遍
    local tpl
    tpl="$(service_template_dir "$CURRENT_NODE" verysync)/config.yaml"
    if [ -f "$tpl" ]; then
        mkdir -p "$dir"
        render_template "$tpl" "${dir}/config.yaml"
        echo "  配置已按本机数据根渲染: ${dir}/config.yaml"
    fi
    echo ""
    echo "  面板会把未安装的 verysync 显示为「待手动安装」, 不计为运行异常。"
    return 0
}

# 在指定节点目录启动 compose (兼容 docker compose / docker-compose)
# 数据根来自 lib-services.sh: oc_data_root() —— 不再拼 /mnt/sd
start_compose() {
    local role=$1
    # 角色 -> 节点名 (从清单反查, 不硬编码 wk-xxx 名字)
    local node=""
    case "$role" in
        edge)    node="$(node_name_by_role edge-gateway  || true)" ;;
        iot)     node="$(node_name_by_role iot-core      || true)" ;;
        storage) node="$(node_name_by_role storage-sync  || true)" ;;
    esac
    if [ -z "$node" ]; then
        log_error "清单中未找到角色为 '$role' 的节点 (请检查 inventory/nodes.yaml)"
        return 1
    fi

    local dir
    dir="$(node_data_dir "$node")"
    if [ ! -d "$dir" ]; then
        log_error "目录不存在: $dir (请先运行 ./scripts/deploy.sh 分发配置)"
        return 1
    fi
    if [ ! -f "${dir}/docker-compose.yml" ]; then
        log_error "未找到 docker-compose.yml: ${dir}"
        return 1
    fi
    ( cd "$dir" && { docker compose up -d 2>/dev/null || docker-compose up -d; } )
    docker ps --format "table {{.Names}}\t{{.Status}}"
}

start_edge() {
    log_info "启动边缘网关节点的 Docker 服务..."
    start_compose "edge"
}

start_iot() {
    log_info "启动 IoT 节点的 Docker 服务..."
    start_compose "iot"
}

start_storage() {
    log_info "启动存储节点的 Docker 服务..."
    start_compose "storage"
}

# 列出各服务安装态 (供 init.sh 与运维排查使用)
list_installed() {
    # 表头是中文, **不能**用 %-Ns 填充 —— printf 按字节补空格, 一个汉字 3 字节,
    # 于是"节点"被补成 14 字节宽却只占 6 个显示列, 整行右移错位。
    # 表头用固定分隔符, 数据行 (纯 ASCII + 单字"是/否") 才用 %-Ns。
    printf '%s\n' "节点           服务           已安装     可选     安装方式"
    printf '%s\n' "-------------- -------------- ---------- -------- --------"
    local n s inst opt imode port
    # services_status_table 输出固定 6 列 (空值以 "-" 占位, 避免 IFS 折叠
    # 空字段导致列错位), 这里按 6 列读, 只用前 5 列。
    while IFS=$'\t' read -r n s inst opt imode port; do
        local im iopt
        [ "$inst" = "1" ] && im="是" || im="否"
        [ "$opt"  = "1" ] && iopt="是" || iopt="否"
        [ "$imode" = "-" ] && imode="自动"
        printf '%-14s %-14s %-10s %-8s %s\n' "$n" "$s" "$im" "$iopt" "$imode"
    done < <(services_status_table)
    echo ""
    echo "组网模式: $(network_mode_label)"
    echo "WireGuard: $([ "$(wg_enabled)" = "1" ] && echo 启用 || echo 未启用)"
}

case "${1:-}" in
    mihomo)      install_mihomo ;;
    xiaomusic)   install_xiaomusic ;;
    migpt)       install_migpt ;;
    verysync)    install_verysync ;;
    # 组网模式关闭 WireGuard 时, 明确拒绝而不是装出一个不可用的环境
    wireguard)
        if [ "$(wg_enabled)" = "1" ]; then
            log_info "WireGuard 由 edges 节点的容器提供 (docker compose up -d 即可), 无需原生安装"
            log_info "配置生成: ./scripts/wireguard-setup.sh"
        else
            log_warn "当前组网模式为 $(network_mode), 未启用 WireGuard, 跳过安装"
            log_info "如需启用: 在 inventory/nodes.yaml 设 network.mode: mixed (或 wireguard)"
        fi
        ;;
    all-native)
        # 单个原生服务失败不应中断其余安装 (set -e 下需显式容错)
        install_mihomo    || log_warn "mihomo 安装失败"
        install_xiaomusic || log_warn "xiaomusic 安装失败"
        install_migpt     || log_warn "migpt 依赖安装失败"
        install_verysync
        ;;
    edge)        start_edge ;;
    iot)         start_iot ;;
    storage)     start_storage ;;
    list-installed|status) list_installed ;;
    all-docker)
        # 单个节点通常只跑其中一个, 任一失败不应中断其余
        start_edge    || log_warn "边缘节点启动失败"
        start_iot     || log_warn "IoT 节点启动失败"
        start_storage || log_warn "存储节点启动失败"
        ;;
    *)           usage ;;
esac
