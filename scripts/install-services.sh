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

usage() {
    cat << EOF
用法: $0 <服务名>

可安装的服务 (原生二进制):
  mihomo       - Clash Meta 代理
  xiaomusic    - 小米音乐
  migpt        - AI 助手 (proxy.py)
  verysync     - 微力同步
  all-native   - 安装所有原生服务

Docker 服务:
  edge         - NODE-01 全部 Docker 服务
  iot          - NODE-02 全部 Docker 服务
  storage      - NODE-03 全部 Docker 服务
  all-docker   - 安装全部

示例:
  $0 mihomo            # 安装 Clash
  $0 xiaomusic         # 安装 xiaomusic
  $0 edge              # 启动 NODE-01 所有容器
  $0 all-native        # 安装所有原生二进制
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
    log_warn "verysync 需要从官网手动下载"
    log_warn "访问 https://www.verysync.com/download 获取 Linux ARM 版本"
}

# 在指定节点目录启动 compose (兼容 docker compose / docker-compose)
start_compose() {
    local node=$1
    local dir="${DATA_ROOT:-/mnt/sd/srv}/${node}"
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
    log_info "启动 NODE-01 Docker 服务..."
    start_compose "wk-edge-01"
}

start_iot() {
    log_info "启动 NODE-02 Docker 服务..."
    start_compose "wk-iot-02"
}

start_storage() {
    log_info "启动 NODE-03 Docker 服务..."
    start_compose "wk-storage-03"
}

case "${1:-}" in
    mihomo)      install_mihomo ;;
    xiaomusic)   install_xiaomusic ;;
    migpt)       install_migpt ;;
    verysync)    install_verysync ;;
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
    all-docker)
        # 单个节点通常只跑其中一个, 任一失败不应中断其余
        start_edge    || log_warn "NODE-01 启动失败"
        start_iot     || log_warn "NODE-02 启动失败"
        start_storage || log_warn "NODE-03 启动失败"
        ;;
    *)           usage ;;
esac
