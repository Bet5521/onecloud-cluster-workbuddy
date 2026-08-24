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

install_mihomo() {
    log_info "安装 mihomo (Clash Meta)..."
    local VER
    VER=$(curl -sL https://api.github.com/repos/MetaCubeX/mihomo/releases/latest | grep '"tag_name"' | sed -E 's/.*"([^"]+)".*/\1/')
    if [ -n "$VER" ]; then
        curl -sL "https://github.com/MetaCubeX/mihomo/releases/download/${VER}/mihomo-linux-armv7-${VER}.gz" \
            | gunzip > /usr/local/bin/mihomo
        chmod +x /usr/local/bin/mihomo
        log_info "mihomo 版本: $($(which mihomo) -v 2>&1 | head -1 || echo 'ok')"
    else
        log_error "下载失败, 请手动安装"
    fi
}

install_xiaomusic() {
    log_info "安装 xiaomusic..."
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
    if [ -f "$tmpdir/xiaomusic" ]; then
        mv "$tmpdir/xiaomusic" /usr/local/bin/
        chmod +x /usr/local/bin/xiaomusic
        log_info "xiaomusic 已安装"
    else
        log_error "下载包结构异常, 请手动安装"
    fi
    rm -rf "$tmpdir"
}

install_migpt() {
    log_info "安装 migpt 轻量代理..."
    pip3 install flask flask-cors pyyaml requests 2>/dev/null || \
        apt install -y python3-flask python3-pip
    log_info "migpt proxy.py 已就绪, 使用 systemd 运行"
}

install_verysync() {
    log_warn "verysync 需要从官网手动下载"
    log_warn "访问 https://www.verysync.com/download 获取 Linux ARM 版本"
}

start_edge() {
    log_info "启动 NODE-01 Docker 服务..."
    cd /mnt/sd/srv/wk-edge-01
    docker compose up -d
    docker ps --format "table {{.Names}}\t{{.Status}}"
}

start_iot() {
    log_info "启动 NODE-02 Docker 服务..."
    cd /mnt/sd/srv/wk-iot-02
    docker compose up -d
    docker ps --format "table {{.Names}}\t{{.Status}}"
}

start_storage() {
    log_info "启动 NODE-03 Docker 服务..."
    cd /mnt/sd/srv/wk-storage-03
    docker compose up -d
    docker ps --format "table {{.Names}}\t{{.Status}}"
}

case "${1:-}" in
    mihomo)      install_mihomo ;;
    xiaomusic)   install_xiaomusic ;;
    migpt)       install_migpt ;;
    verysync)    install_verysync ;;
    all-native)
        install_mihomo
        install_xiaomusic
        install_migpt
        install_verysync
        ;;
    edge)        start_edge ;;
    iot)         start_iot ;;
    storage)     start_storage ;;
    all-docker)
        start_edge
        start_iot
        start_storage
        ;;
    *)           usage ;;
esac
