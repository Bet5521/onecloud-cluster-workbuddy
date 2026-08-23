#!/bin/bash
# ============================================================
# OneCloud Cluster - 统一安装部署脚本 (setup.sh)
# 功能: 端口冲突检测 / 多选批量安装 / U盘自动挂载 / SD卡自动挂载
# 用法: sudo bash setup.sh
# ============================================================
set -u

# ---- 颜色 ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# ---- 日志 ----
log_info()    { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*"; }
log_step()    { echo -e "\n${BLUE}${BOLD}==> $*${NC}"; }
log_success() { echo -e "${GREEN}${BOLD}✓ $*${NC}"; }

# ---- 全局配置 ----
DATA_DIR="/mnt/sd/srv"          # 数据根目录(优先 SD 卡)
[ -d /mnt/sd ] || DATA_DIR="/opt/onecloud/srv"
COMPOSE_DIR="${DATA_DIR}"       # docker-compose 目录
TZ="Asia/Shanghai"
PUID=1000
PGID=1000

# ============================================================
# 工具函数
# ============================================================

# 检测是否 root
check_root() {
    if [ "$(id -u)" -ne 0 ]; then
        log_error "请使用 root 运行: sudo bash $0"
        exit 1
    fi
}

# 检测命令是否存在
has_cmd() { command -v "$1" &>/dev/null; }

# 确保安装了某个包
ensure_pkg() {
    local pkg=$1
    if ! dpkg -l "$pkg" &>/dev/null 2>&1; then
        apt-get install -y "$pkg" 2>/dev/null || true
    fi
}

# 检测端口是否被占用
# 参数: $1=端口号 $2=协议(tcp/udp)
check_port() {
    local port=$1 proto=${2:-tcp}
    if has_cmd ss; then
        ss -tuln 2>/dev/null | grep -qE "[:.]${port}\b"
    elif has_cmd netstat; then
        netstat -tuln 2>/dev/null | grep -qE "[:.]${port}\b"
    else
        ensure_pkg net-tools
        netstat -tuln 2>/dev/null | grep -qE "[:.]${port}\b"
    fi
}

# 获取占用端口的进程
get_port_holder() {
    local port=$1
    if has_cmd ss; then
        ss -tulnp 2>/dev/null | grep -E "[:.]${port}\b" | awk '{print $NF}' | head -1
    elif has_cmd netstat; then
        netstat -tulnp 2>/dev/null | grep -E "[:.]${port}\b" | awk '{print $NF}' | head -1
    fi
}

# 检测 Docker 是否可用
ensure_docker() {
    if ! has_cmd docker; then
        log_warn "Docker 未安装, 正在安装..."
        curl -fsSL https://get.docker.com | bash 2>/dev/null || {
            apt-get update
            apt-get install -y docker.io docker-compose
        }
        systemctl enable --now docker
    fi
    if ! docker info &>/dev/null; then
        log_error "Docker 未运行, 请检查: systemctl start docker"
        return 1
    fi
    log_success "Docker 可用"
}

# 确保有 jq(用于 JSON 处理)
ensure_tools() {
    local need=()
    has_cmd jq     || need+=("jq")
    has_cmd curl    || need+=("curl")
    has_cmd wget    || need+=("wget")
    has_cmd parted  || need+=("parted")
    has_cmd dosfstools 2>/dev/null || need+=("dosfstools")
    if [ ${#need[@]} -gt 0 ]; then
        apt-get update -qq
        apt-get install -y "${need[@]}" 2>/dev/null || true
    fi
}

# 获取 GitHub 最新版本号
get_latest_release() {
    local repo=$1
    curl -sL "https://api.github.com/repos/${repo}/releases/latest" 2>/dev/null \
        | grep '"tag_name"' | sed -E 's/.*"([^"]+)".*/\1/'
}

# ============================================================
# 服务定义表
# 格式: id|显示名|分类|类型(disk/docker/native)|端口列表|安装函数|描述
# 端口格式: port/proto,port/proto,...
# ============================================================

SERVICE_COUNT=0
declare -a SVC_ID SVC_NAME SVC_CAT SVC_TYPE SVC_PORTS SVC_FUNC SVC_DESC

add_service() {
    SVC_ID[SERVICE_COUNT]=$1
    SVC_NAME[SERVICE_COUNT]=$2
    SVC_CAT[SERVICE_COUNT]=$3
    SVC_TYPE[SERVICE_COUNT]=$4
    SVC_PORTS[SERVICE_COUNT]=$5
    SVC_FUNC[SERVICE_COUNT]=$6
    SVC_DESC[SERVICE_COUNT]=$7
    ((SERVICE_COUNT++))
}

# ---- 磁盘管理 ----
add_service "usb_mount"    "U盘自动挂载"       "磁盘管理" "disk"    ""                                          "mount_usb"        "自动检测U盘并挂载, 写入fstab"
add_service "sd_mount"     "SD卡自动挂载"      "磁盘管理" "disk"    ""                                          "mount_sd"         "检测SD卡并格式化挂载, 可迁移Docker数据"

# ---- Docker 服务 ----
add_service "adguard"     "AdGuard Home"      "Docker服务" "docker" "53/tcp,53/udp,3000/tcp"                   "install_adguard"   "DNS 广告过滤, 网页管理"
add_service "cloudflared" "Cloudflare Tunnel" "Docker服务" "docker" ""                                          "install_cloudflared" "内网穿透, 无需公网IP"
add_service "wireguard"   "WireGuard VPN"     "Docker服务" "docker" "51820/udp"                               "install_wireguard"  "轻量级 VPN, 支持多客户端"
add_service "memos"       "Memos 备忘录"      "Docker服务" "docker" "5230/tcp"                               "install_memos"      "轻量级笔记/备忘录服务"
add_service "homeassistant" "Home Assistant"   "Docker服务" "docker" "8123/tcp"                              "install_homeassistant" "开源智能家居平台"
add_service "piwigo"      "Piwigo 相册"        "Docker服务" "docker" "8080/tcp"                              "install_piwigo"     "网页相册管理系统"
add_service "syncthing"   "Syncthing 同步"     "Docker服务" "docker" "8384/tcp,22000/tcp,22000/udp,21027/udp" "install_syncthing"  "去中心化文件同步"
add_service "aria2"       "aria2 下载"         "Docker服务" "docker" "6800/tcp,6888/tcp,6888/udp"            "install_aria2"      "多线程下载工具, 含AriaNg"
add_service "cupsd"       "CUPS 打印服务"      "Docker服务" "docker" "631/tcp"                                "install_cupsd"      "网络打印服务器"
add_service "cups_web"    "CUPS-Web 管理"      "Docker服务" "docker" "632/tcp"                                "install_cups_web"   "CUPS 网页管理界面"

# ---- 原生服务 ----
add_service "clash"       "Clash/mihomo 代理"   "原生服务" "native" "9090/tcp"                               "install_clash"      "Clash Meta 代理, ARM 优化"
add_service "xiaomusic"   "xiaomusic 小爱音乐" "原生服务" "native" "8081/tcp"                               "install_xiaomusic"  "小爱音箱音乐播放器"
add_service "migpt"       "migpt AI助手"       "原生服务" "native" "8082/tcp"                               "install_migpt"      "小米AI对话代理"
add_service "verysync"    "verysync 微力同步"   "原生服务" "native" "19900/tcp"                              "install_verysync"   "高效文件同步"

# ---- 管理面板 ----
add_service "panel"       "集群控制面板"        "管理面板" "native" "9000/tcp"                               "install_panel"      "Flask 集群管理面板"

# ============================================================
# 端口冲突检测
# ============================================================

# 检测所有选中服务的端口, 返回冲突列表
check_port_conflicts() {
    local -a selected_ids=("$@")
    local conflicts=0
    local checked=()  # 已检测过的端口(避免重复检测)

    echo ""
    log_step "端口冲突检测"
    echo "------------------------------------------"

    for id in "${selected_ids[@]}"; do
        local ports=""
        local name=""
        for ((i=0; i<SERVICE_COUNT; i++)); do
            if [ "${SVC_ID[$i]}" = "$id" ]; then
                ports="${SVC_PORTS[$i]}"
                name="${SVC_NAME[$i]}"
                break
            fi
        done

        [ -z "$ports" ] && continue

        # 解析端口列表
        IFS=',' read -ra port_list <<< "$ports"
        for entry in "${port_list[@]}"; do
            local port proto
            port="${entry%%/*}"
            proto="${entry#*/}"
            [ "$proto" = "$entry" ] && proto="tcp"

            # 跳过已检测的端口
            local key="${port}/${proto}"
            local already=0
            for k in "${checked[@]:-}"; do
                [ "$k" = "$key" ] && { already=1; break; }
            done
            [ $already -eq 1 ] && continue
            checked+=("$key")

            if check_port "$port" "$proto"; then
                local holder
                holder=$(get_port_holder "$port")
                echo -e "  ${RED}✗${NC} [${proto^^}] ${port}  ${name}  <- 冲突! 占用: ${holder:-未知}"
                ((conflicts++))
            else
                echo -e "  ${GREEN}✓${NC} [${proto^^}] ${port}  ${name}"
            fi
        done
    done

    echo "------------------------------------------"
    return $conflicts
}

# ============================================================
# 磁盘挂载 - U盘
# ============================================================

mount_usb() {
    log_step "U盘自动挂载"

    echo ""
    echo "检测可用的块设备..."
    echo "------------------------------------------"

    # 列出所有块设备, 排除系统盘和 SD 卡
    local devices=()
    local idx=0
    while read -r name size type model; do
        [ "$type" = "disk" ] || continue
        # 跳过 SD 卡 (mmcblk) 和系统盘
        [[ "$name" == mmcblk* ]] && continue
        [[ "$name" == loop* ]] && continue
        [[ "$name" == sr* ]] && continue

        # 检查是否已挂载
        local mounted
        mounted=$(lsblk -n -o MOUNTPOINT "/dev/$name" 2>/dev/null | head -1)

        local dev_path="/dev/$name"
        devices[$idx]="$dev_path"
        printf "  [%d] %-12s %-8s %s %s\n" "$idx" "$dev_path" "$size" "${model:-}" \
            "$([ -n "$mounted" ] && echo "(已挂载: $mounted)" || echo "")"
        ((idx++))
    done < <(lsblk -n -o NAME,SIZE,TYPE,MODEL 2>/dev/null)

    if [ $idx -eq 0 ]; then
        log_warn "未检测到可用的 U盘/移动硬盘"
        return 1
    fi

    echo "------------------------------------------"
    local choice
    read -p "选择要挂载的设备编号 (0-$((idx-1))): " choice
    if ! [[ "$choice" =~ ^[0-9]+$ ]] || [ "$choice" -ge "$idx" ]; then
        log_error "无效选择"
        return 1
    fi

    local dev="${devices[$choice]}"
    log_info "选择设备: $dev"

    # 检查是否已有分区
    local partitions
    partitions=$(lsblk -ln -o NAME "$dev" 2>/dev/null | grep -E "^${dev##*/}p?[0-9]+" | head -1)

    local target_part
    if [ -z "$partitions" ]; then
        log_warn "设备无分区, 开始创建..."
        parted -s "$dev" mklabel gpt 2>/dev/null || parted -s "$dev" mklabel msdos
        parted -s "$dev" mkpart primary ext4 1MiB 100%
        sleep 2
        # 自动找到新创建的分区
        target_part=$(lsblk -ln -o NAME "$dev" 2>/dev/null | grep -E "^${dev##*/}p?[0-9]+" | head -1)
        target_part="/dev/$target_part"
        mkfs.ext4 -F "$target_part"
    else
        target_part="/dev/$partitions"
        # 检查文件系统
        local fstype
        fstype=$(lsblk -ln -o FSTYPE "$target_part" 2>/dev/null | head -1)
        if [ -z "$fstype" ]; then
            log_warn "分区未格式化, 格式化为 ext4..."
            mkfs.ext4 -F "$target_part"
        else
            log_info "已有文件系统: $fstype"
            local fmt
            read -p "是否重新格式化? (数据将丢失!) [y/N] " fmt
            [[ "$fmt" =~ ^[Yy]$ ]] && mkfs.ext4 -F "$target_part"
        fi
    fi

    # 挂载
    local mountpoint="/mnt/usb"
    local uuid
    uuid=$(blkid -s UUID -o value "$target_part" 2>/dev/null)
    mkdir -p "$mountpoint"

    if ! mountpoint -q "$mountpoint"; then
        mount "$target_part" "$mountpoint"
    fi

    # 写入 fstab (用 UUID 确保稳定)
    if [ -n "$uuid" ] && ! grep -q "$uuid" /etc/fstab; then
        echo "UUID=$uuid $mountpoint ext4 defaults,noatime,nofail 0 2" >> /etc/fstab
        log_success "已写入 fstab (UUID=$uuid)"
    fi

    log_success "U盘已挂载到 $mountpoint"
    df -h "$mountpoint" | tail -1

    # 可选: 创建服务数据目录
    read -p "是否在此设备创建服务数据目录 ($mountpoint/srv)? [Y/n] " mkdata
    [[ "$mkdata" =~ ^[Nn]$ ]] || {
        mkdir -p "$mountpoint/srv"
        log_info "数据目录: $mountpoint/srv"
        # 更新全局 DATA_DIR
        DATA_DIR="$mountpoint/srv"
        log_info "后续服务数据将安装到 $DATA_DIR"
    }
}

# ============================================================
# 磁盘挂载 - SD卡
# ============================================================

mount_sd() {
    log_step "SD卡自动挂载"

    echo ""
    echo "检测 SD 卡设备..."
    echo "------------------------------------------"

    local sd_devs=()
    local idx=0
    while read -r name size model; do
        [[ "$name" == mmcblk* ]] || continue
        [[ "$name" == *boot* ]] && continue
        # 只要 disk 级别
        echo "$name" | grep -qE '^mmcblk[0-9]+$' || continue

        sd_devs[$idx]="/dev/$name"
        printf "  [%d] %-14s %-8s %s\n" "$idx" "/dev/$name" "$size" "${model:-}"
        ((idx++))
    done < <(lsblk -n -o NAME,SIZE,MODEL 2>/dev/null)

    if [ $idx -eq 0 ]; then
        log_warn "未检测到 SD 卡设备"
        log_info "玩客云内置 eMMC 通常为 mmcblk2, SD 卡为 mmcblk1"
        local manual
        read -p "手动输入 SD 卡设备名 (如 mmcblk1, 留空跳过): " manual
        [ -z "$manual" ] && return 1
        sd_devs[0]="/dev/$manual"
        idx=1
    fi

    echo "------------------------------------------"
    local choice=0
    [ $idx -gt 1 ] && read -p "选择 SD 卡编号 (0-$((idx-1))): " choice
    local dev="${sd_devs[$choice]}"
    log_info "选择设备: $dev"

    local mountpoint="/mnt/sd"
    local part="${dev}p1"

    # 检查分区是否存在
    if [ ! -b "$part" ]; then
        part="${dev}1"  # 某些设备命名方式
    fi

    if [ ! -b "$part" ]; then
        log_warn "SD 卡无分区, 开始创建..."
        parted -s "$dev" mklabel gpt 2>/dev/null || parted -s "$dev" mklabel msdos
        parted -s "$dev" mkpart primary ext4 1MiB 100%
        sleep 2
        part="${dev}p1"
        [ ! -b "$part" ] && part="${dev}1"
        mkfs.ext4 -F "$part"
    else
        local fstype
        fstype=$(lsblk -ln -o FSTYPE "$part" 2>/dev/null | head -1)
        if [ -z "$fstype" ]; then
            log_warn "SD 卡分区未格式化, 格式化为 ext4..."
            mkfs.ext4 -F "$part"
        else
            log_info "已有文件系统: $fstype"
            local fmt
            read -p "是否重新格式化? (数据将丢失!) [y/N] " fmt
            [[ "$fmt" =~ ^[Yy]$ ]] && mkfs.ext4 -F "$part"
        fi
    fi

    # 挂载
    mkdir -p "$mountpoint"
    if ! mountpoint -q "$mountpoint"; then
        mount "$part" "$mountpoint"
    fi

    # 写入 fstab
    local uuid
    uuid=$(blkid -s UUID -o value "$part" 2>/dev/null)
    if [ -n "$uuid" ] && ! grep -q "$uuid" /etc/fstab; then
        echo "UUID=$uuid $mountpoint ext4 defaults,noatime 0 2" >> /etc/fstab
        log_success "已写入 fstab (UUID=$uuid)"
    fi

    log_success "SD卡已挂载到 $mountpoint"
    df -h "$mountpoint" | tail -1

    # 创建标准目录结构
    mkdir -p "$mountpoint/srv" "$mountpoint/docker" "$mountpoint/backups"
    log_info "已创建目录: srv/ docker/ backups/"

    # 可选: 迁移 Docker 数据到 SD 卡
    if has_cmd docker; then
        read -p "是否迁移 Docker 数据到 SD 卡? [Y/n] " migrate
        if [[ ! "$migrate" =~ ^[Nn]$ ]]; then
            log_info "迁移 Docker 数据..."
            systemctl stop docker 2>/dev/null || true
            [ -d /var/lib/docker ] && rsync -aP /var/lib/docker/ "$mountpoint/docker/"
            mkdir -p /etc/docker
            cat > /etc/docker/daemon.json << EOF
{
  "data-root": "$mountpoint/docker",
  "log-driver": "json-file",
  "log-opts": { "max-size": "5m", "max-file": "2" }
}
EOF
            systemctl start docker
            log_success "Docker 数据已迁移到 $mountpoint/docker"
        fi
    fi

    # 更新全局数据目录
    DATA_DIR="$mountpoint/srv"
    log_info "后续服务数据将安装到 $DATA_DIR"
}

# ============================================================
# Docker 服务安装函数
# ============================================================

# 通用: 确保服务目录存在
ensure_svc_dir() {
    local svc=$1
    mkdir -p "${DATA_DIR}/${svc}"
}

# 通用: 停止并移除旧容器
remove_old_container() {
    local name=$1
    docker rm -f "$name" 2>/dev/null || true
}

install_adguard() {
    log_info "安装 AdGuard Home..."
    ensure_docker
    ensure_svc_dir "adguard"
    mkdir -p "${DATA_DIR}/adguard/work" "${DATA_DIR}/adguard/conf"
    remove_old_container "adguard"
    docker run -d --name adguard \
        --restart unless-stopped \
        -p 53:53/tcp -p 53:53/udp -p 3000:3000 \
        -e TZ=$TZ \
        -v "${DATA_DIR}/adguard/work:/opt/adguardhome/work" \
        -v "${DATA_DIR}/adguard/conf:/opt/adguardhome/conf" \
        adguard/adguardhome:latest
    log_success "AdGuard Home 已启动 -> http://$(hostname -I | awk '{print $1}'):3000"
}

install_cloudflared() {
    log_info "安装 Cloudflare Tunnel..."
    ensure_docker
    ensure_svc_dir "cloudflared"
    remove_old_container "cloudflared"
    echo "请提前准备 Cloudflare Tunnel Token"
    read -p "输入 Tunnel Token (留空则仅创建容器, 稍后配置): " token
    if [ -n "$token" ]; then
        docker run -d --name cloudflared \
            --restart unless-stopped \
            -e TZ=$TZ \
            cloudflare/cloudflared:latest tunnel --no-autoupdate run --token "$token"
    else
        docker run -d --name cloudflared \
            --restart unless-stopped \
            -e TZ=$TZ \
            -v "${DATA_DIR}/cloudflared:/etc/cloudflared" \
            cloudflare/cloudflared:latest tunnel --no-autoupdate run
    fi
    log_success "Cloudflare Tunnel 已启动"
}

install_wireguard() {
    log_info "安装 WireGuard..."
    ensure_docker
    ensure_svc_dir "wireguard/config"
    remove_old_container "wireguard"

    read -p "输入公网域名/IP (留空用 yourdomain.com): " serverurl
    serverurl="${serverurl:-yourdomain.com}"

    read -p "客户端数量 (默认3): " peers
    peers="${peers:-3}"

    docker run -d --name wireguard \
        --restart unless-stopped \
        --cap-add NET_ADMIN --cap-add SYS_MODULE \
        --sysctl net.ipv4.conf.all.src_valid_mark=1 \
        -p 51820:51820/udp \
        -e TZ=$TZ -e PUID=$PUID -e PGID=$PGID \
        -e SERVERURL="$serverurl" -e SERVERPORT=51820 \
        -e PEERS="$peers" -e ALLOWEDIPS=0.0.0.0/0 \
        -e LOG_CONFS=true \
        -v "${DATA_DIR}/wireguard/config:/config" \
        -v /lib/modules:/lib/modules \
        linuxserver/wireguard:latest
    log_success "WireGuard 已启动 -> 端口 51820/udp"
    log_info "客户端配置在: ${DATA_DIR}/wireguard/config/peer*/"
}

install_memos() {
    log_info "安装 Memos..."
    ensure_docker
    ensure_svc_dir "memos/data"
    remove_old_container "memos"
    docker run -d --name memos \
        --restart unless-stopped \
        -p 5230:5230 \
        -e TZ=$TZ -e MEMOS_PORT=5230 -e MEMOS_DRIVER=sqlite \
        -v "${DATA_DIR}/memos/data:/var/opt/memos" \
        neosmemo/memos:stable
    log_success "Memos 已启动 -> http://$(hostname -I | awk '{print $1}'):5230"
}

install_homeassistant() {
    log_info "安装 Home Assistant..."
    ensure_docker
    ensure_svc_dir "homeassistant"
    remove_old_container "homeassistant"

    # Home Assistant 使用 host 网络模式
    docker run -d --name homeassistant \
        --restart unless-stopped \
        --network host \
        --privileged \
        -e TZ=$TZ \
        -v "${DATA_DIR}/homeassistant:/config" \
        -v /run/dbus:/run/dbus:ro \
        ghcr.io/home-assistant/home-assistant:stable 2>/dev/null || \
    docker run -d --name homeassistant \
        --restart unless-stopped \
        -p 8123:8123 \
        -e TZ=$TZ \
        -v "${DATA_DIR}/homeassistant:/config" \
        homeassistant/home-assistant:stable
    log_success "Home Assistant 已启动 -> http://$(hostname -I | awk '{print $1}'):8123"
}

install_piwigo() {
    log_info "安装 Piwigo..."
    ensure_docker
    ensure_svc_dir "piwigo/config" "piwigo/gallery"
    remove_old_container "piwigo"
    docker run -d --name piwigo \
        --restart unless-stopped \
        -p 8080:80 \
        -e TZ=$TZ -e PUID=$PUID -e PGID=$PGID \
        -v "${DATA_DIR}/piwigo/config:/config" \
        -v "${DATA_DIR}/piwigo/gallery:/gallery" \
        linuxserver/piwigo:latest
    log_success "Piwigo 已启动 -> http://$(hostname -I | awk '{print $1}'):8080"
}

install_syncthing() {
    log_info "安装 Syncthing..."
    ensure_docker
    ensure_svc_dir "syncthing/config" "syncthing/data"
    remove_old_container "syncthing"
    docker run -d --name syncthing \
        --restart unless-stopped \
        -p 8384:8384 -p 22000:22000/tcp -p 22000:22000/udp -p 21027:21027/udp \
        -e TZ=$TZ -e PUID=$PUID -e PGID=$PGID \
        -v "${DATA_DIR}/syncthing/config:/var/syncthing/config" \
        -v "${DATA_DIR}/syncthing/data:/var/syncthing/data" \
        syncthing/syncthing:latest
    log_success "Syncthing 已启动 -> http://$(hostname -I | awk '{print $1}'):8384"
}

install_aria2() {
    log_info "安装 aria2..."
    ensure_docker
    ensure_svc_dir "aria2/config" "aria2/downloads"
    remove_old_container "aria2"

    local secret
    read -p "设置 RPC 密钥 (留空随机生成): " secret
    [ -z "$secret" ] && secret=$(head -c 16 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 16)
    log_info "RPC 密钥: $secret"

    docker run -d --name aria2 \
        --restart unless-stopped \
        -p 6800:6800 -p 6888:6888 -p 6888:6888/udp \
        -e TZ=$TZ -e PUID=$PUID -e PGID=$PGID \
        -e RPC_SECRET="$secret" -e RPC_PORT=6800 \
        -e LISTEN_PORT=6888 -e DISK_CACHE=64M \
        -e UPDATE_TRACKERS=true \
        -v "${DATA_DIR}/aria2/config:/config" \
        -v "${DATA_DIR}/aria2/downloads:/downloads" \
        p3terx/aria2-pro:latest
    log_success "aria2 已启动 -> RPC: http://$(hostname -I | awk '{print $1}'):6800"
    log_info "建议配合 AriaNg 前端使用"
}

install_cupsd() {
    log_info "安装 CUPS 打印服务..."
    ensure_docker
    ensure_svc_dir "cupsd/config" "cupsd/printers"
    remove_old_container "cupsd"

    local cups_user="admin"
    local cups_pass
    read -p "设置 CUPS 管理密码 (留空用 changeme): " cups_pass
    cups_pass="${cups_pass:-changeme}"

    docker run -d --name cupsd \
        --restart unless-stopped \
        -p 631:631 \
        --privileged \
        -e TZ=$TZ \
        -e ADMIN_USER="$cups_user" \
        -e ADMIN_PASSWORD="$cups_pass" \
        -v "${DATA_DIR}/cupsd/config:/etc/cups" \
        -v "${DATA_DIR}/cupsd/printers:/etc/cups/printers" \
        -v /dev:/dev \
        olbat/cupsd:latest 2>/dev/null || \
    docker run -d --name cupsd \
        --restart unless-stopped \
        -p 631:631 \
        -e TZ=$TZ \
        -v "${DATA_DIR}/cupsd/config:/etc/cups" \
        ousia/cupsd:armhf
    log_success "CUPS 已启动 -> https://$(hostname -I | awk '{print $1}'):631"
    log_info "用户: $cups_user  密码: $cups_pass"
}

install_cups_web() {
    log_info "安装 CUPS-Web 管理..."
    ensure_docker
    ensure_svc_dir "cups-web/config"
    remove_old_container "cups-web"

    local cups_host
    read -p "CUPS 服务地址 (留空用本机IP): " cups_host
    cups_host="${cups_host:-$(hostname -I | awk '{print $1}')}"

    docker run -d --name cups-web \
        --restart unless-stopped \
        -p 632:632 \
        -e TZ=$TZ \
        -e CUPS_HOST="$cups_host" \
        -e CUPS_PORT=631 \
        -v "${DATA_DIR}/cups-web/config:/app/config" \
        nkn-ts/cups-web:armhf 2>/dev/null || \
    docker run -d --name cups-web \
        --restart unless-stopped \
        -p 632:632 \
        -e TZ=$TZ \
        -e CUPS_HOST="$cups_host" \
        -e CUPS_PORT=631 \
        nkn-ts/cups-web:latest
    log_success "CUPS-Web 已启动 -> http://$(hostname -I | awk '{print $1}'):632"
}

# ============================================================
# 原生服务安装函数
# ============================================================

install_clash() {
    log_info "安装 Clash (mihomo)..."
    ensure_svc_dir "clash"

    local ver
    ver=$(get_latest_release "MetaCubeX/mihomo")
    if [ -z "$ver" ]; then
        log_error "获取版本失败, 使用最新"
        ver="v1.18.0"
    fi
    log_info "mihomo 版本: $ver"

    local arch="armv7"
    case "$(uname -m)" in
        aarch64|arm64) arch="arm64" ;;
        x86_64)        arch="amd64" ;;
    esac

    curl -sL "https://github.com/MetaCubeX/mihomo/releases/download/${ver}/mihomo-linux-${arch}-${ver}.gz" \
        | gunzip > /usr/local/bin/mihomo
    chmod +x /usr/local/bin/mihomo

    # 生成最小配置
    if [ ! -f "${DATA_DIR}/clash/config.yaml" ]; then
        cat > "${DATA_DIR}/clash/config.yaml" << 'EOF'
# mihomo 最小配置 - 请替换为你的订阅
mixed-port: 7890
external-controller: 0.0.0.0:9090
allow-lan: true
mode: rule
log-level: info

proxies: []
proxy-groups: []
rules:
  - MATCH,DIRECT
EOF
        log_warn "已生成最小配置, 请替换为你的订阅: ${DATA_DIR}/clash/config.yaml"
    fi

    # systemd 服务
    cat > /etc/systemd/system/mihomo.service << EOF
[Unit]
Description=mihomo (Clash Meta)
After=network.target

[Service]
ExecStart=/usr/local/bin/mihomo -d ${DATA_DIR}/clash
Restart=on-failure
RestartSec=5
LimitNOFILE=999999

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now mihomo
    log_success "mihomo 已启动 -> API: http://$(hostname -I | awk '{print $1}'):9090"
    log_info "配置文件: ${DATA_DIR}/clash/config.yaml"
}

install_xiaomusic() {
    log_info "安装 xiaomusic..."
    ensure_svc_dir "xiaomusic"

    local arch="arm"
    case "$(uname -m)" in
        aarch64|arm64) arch="arm64" ;;
        x86_64)        arch="amd64" ;;
    esac

    # 获取下载链接
    local url
    url=$(curl -sL https://api.github.com/repos/hanxi/xiaomusic/releases/latest \
        | grep "browser_download_url" | grep "linux_${arch}" | head -1 \
        | sed -E 's/.*"([^"]+)".*/\1/')

    if [ -n "$url" ]; then
        curl -sL "$url" | tar xz -C /tmp
        mv /tmp/xiaomusic /usr/local/bin/
        chmod +x /usr/local/bin/xiaomusic
    else
        log_error "下载失败, 请手动安装: https://github.com/hanxi/xiaomusic/releases"
        return 1
    fi

    # 配置
    if [ ! -f "${DATA_DIR}/xiaomusic/config.json" ]; then
        cat > "${DATA_DIR}/xiaomusic/config.json" << 'EOF'
{
  "port": 8081,
  "music_path": "/mnt/sd/music",
  "download_path": "/mnt/sd/music/download",
  "hostname": "192.168.1.102",
  "account": "xiaomusic",
  "password": "xiaomusic"
}
EOF
    fi

    mkdir -p /mnt/sd/music/download

    # systemd
    cat > /etc/systemd/system/xiaomusic.service << EOF
[Unit]
Description=xiaomusic
After=network.target

[Service]
ExecStart=/usr/local/bin/xiaomusic -c ${DATA_DIR}/xiaomusic/config.json
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now xiaomusic
    log_success "xiaomusic 已启动 -> http://$(hostname -I | awk '{print $1}'):8081"
}

install_migpt() {
    log_info "安装 migpt AI助手..."
    ensure_svc_dir "migpt"

    # 安装 Python 依赖
    apt-get install -y python3-pip python3-venv 2>/dev/null || true
    pip3 install flask flask-cors pyyaml requests 2>/dev/null || true

    # 生成代理脚本
    cat > "${DATA_DIR}/migpt/proxy.py" << 'PYEOF'
#!/usr/bin/env python3
"""migpt 轻量代理 - 将请求转发到指定 LLM API"""
import json, os, requests
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# 配置: 修改为你的 LLM API
LLM_API_URL = os.environ.get("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "your-api-key")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-3.5-turbo")

@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    resp = requests.post(LLM_API_URL, json=request.json, headers=headers, stream=True)
    return Response(resp.iter_content(chunk_size=8192),
                    content_type=resp.headers.get("Content-Type", "application/json"))

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8082)
PYEOF

    # systemd
    cat > /etc/systemd/system/migpt.service << EOF
[Unit]
Description=migpt AI Proxy
After=network.target

[Service]
ExecStart=/usr/bin/python3 ${DATA_DIR}/migpt/proxy.py
Restart=on-failure
RestartSec=5
Environment=LLM_API_KEY=your-api-key

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now migpt
    log_success "migpt 已启动 -> http://$(hostname -I | awk '{print $1}'):8082"
    log_warn "请修改 LLM_API_KEY: 编辑 /etc/systemd/system/migpt.service"
}

install_verysync() {
    log_info "安装 verysync 微力同步..."
    ensure_svc_dir "verysync"

    log_warn "verysync 需要手动下载二进制"
    log_info "请访问: https://www.verysync.com/download"
    log_info "下载 Linux ARM 版本, 解压到 /usr/local/bin/verysync"

    read -p "是否已下载 verysync 二进制? [y/N] " ready
    if [[ ! "$ready" =~ ^[Yy]$ ]]; then
        log_warn "跳过 verysync 安装, 下载后重新运行"
        return 1
    fi

    [ ! -f /usr/local/bin/verysync ] && {
        read -p "verysync 二进制路径 (如 /tmp/verysync): " binpath
        cp "$binpath" /usr/local/bin/verysync
        chmod +x /usr/local/bin/verysync
    }

    cat > /etc/systemd/system/verysync.service << EOF
[Unit]
Description=verysync
After=network.target

[Service]
ExecStart=/usr/local/bin/verysync -gui-address=0.0.0.0:19900 -config=${DATA_DIR}/verysync
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now verysync
    log_success "verysync 已启动 -> http://$(hostname -I | awk '{print $1}'):19900"
}

install_panel() {
    log_info "安装集群控制面板..."
    ensure_svc_dir "panel"
    local panel_dir="${DATA_DIR}/panel"

    # 安装 Python 依赖
    apt-get install -y python3-pip python3-venv 2>/dev/null || true
    pip3 install flask flask-cors pyyaml requests 2>/dev/null || true

    # 尝试从项目模板复制完整的面板文件
    local script_dir
    script_dir="$(cd "$(dirname "$0")" && pwd)"
    local project_panel="${script_dir}/../panel"

    if [ -d "$project_panel" ] && [ -f "${project_panel}/app.py" ]; then
        log_info "从项目模板复制面板文件..."
        cp -r "$project_panel"/* "$panel_dir/"
    else
        # 生成最小面板应用 (fallback)
        log_warn "项目模板未找到, 生成最小面板..."
        cat > "${panel_dir}/app.py" << 'PYEOF'
#!/usr/bin/env python3
"""OneCloud Cluster 集群控制面板"""
import json, os, subprocess
from datetime import datetime
from flask import Flask, jsonify, render_template

app = Flask(__name__)
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except:
        return {"cluster_name": "OneCloud Cluster", "nodes": []}

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def status():
    config = load_config()
    nodes = []
    for node in config.get("nodes", []):
        nodes.append({
            "name": node["name"],
            "display_name": node.get("display_name", node["name"]),
            "ip": node["ip"],
            "wg_ip": node.get("wg_ip", "-"),
            "online": False,
            "services": node.get("services", [])
        })
    return jsonify({
        "cluster_name": config.get("cluster_name", "OneCloud Cluster"),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "nodes": nodes
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9000)
PYEOF

        # 生成配置
        if [ ! -f "${panel_dir}/config.json" ]; then
            cat > "${panel_dir}/config.json" << 'EOF'
{
  "cluster_name": "OneCloud Cluster",
  "nodes": [
    {"name": "wk-edge-01", "display_name": "Edge Gateway", "ip": "192.168.1.101", "wg_ip": "10.8.0.101"},
    {"name": "wk-iot-02", "display_name": "IoT Core", "ip": "192.168.1.102", "wg_ip": "10.8.0.102"},
    {"name": "wk-storage-03", "display_name": "Storage & Sync", "ip": "192.168.1.103", "wg_ip": "10.8.0.103"}
  ]
}
EOF
        fi

        # 生成最小前端
        mkdir -p "${panel_dir}/templates" "${panel_dir}/static/css"
        cat > "${panel_dir}/templates/index.html" << 'EOF'
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>OneCloud Cluster</title>
<link rel="stylesheet" href="/static/css/style.css">
</head>
<body>
<h1>OneCloud Cluster</h1>
<div id="nodes">加载中...</div>
<script src="/static/js/app.js"></script>
</body>
</html>
EOF
        cat > "${panel_dir}/static/css/style.css" << 'EOF'
body{font-family:sans-serif;background:#1a1a2e;color:#e0e0e0;padding:20px}
.node{background:rgba(255,255,255,0.06);border-radius:12px;padding:16px;margin:8px 0}
.online{color:#38ef7d} .offline{color:#f5576c}
EOF
        cat > "${panel_dir}/static/js/app.js" << 'EOF'
fetch("/api/status").then(r=>r.json()).then(d=>{
  document.getElementById("nodes").innerHTML=d.nodes.map(n=>
    `<div class="node"><h3>${n.display_name}</h3><p>${n.ip}</p></div>`
  ).join("");
});
EOF
    fi

    # systemd
    cat > /etc/systemd/system/onecloud-panel.service << EOF
[Unit]
Description=OneCloud Cluster Panel
After=network.target

[Service]
ExecStart=/usr/bin/python3 ${panel_dir}/app.py
Restart=on-failure
RestartSec=5
WorkingDirectory=${panel_dir}

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now onecloud-panel
    log_success "面板已启动 -> http://$(hostname -I | awk '{print $1}'):9000"
}

# ============================================================
# 交互式多选菜单
# ============================================================

# 当前选中状态
declare -a SELECTED
for ((i=0; i<SERVICE_COUNT; i++)); do
    SELECTED[$i]=0
done

# 显示菜单
draw_menu() {
    local cursor=$1
    clear
    echo -e "${BOLD}${CYAN}"
    echo "╔══════════════════════════════════════════════════════════╗"
    echo "║       OneCloud Cluster - 统一安装部署脚本                 ║"
    echo "╚══════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
    echo -e "${DIM}操作: ↑↓移动  空格选择  a全选  n全不选  回车确认安装  q退出${NC}"
    echo ""

    local current_cat=""
    for ((i=0; i<SERVICE_COUNT; i++)); do
        # 分类标题
        if [ "${SVC_CAT[$i]}" != "$current_cat" ]; then
            current_cat="${SVC_CAT[$i]}"
            echo -e "  ${BLUE}${BOLD}── ${current_cat} ──${NC}"
        fi

        # 选中标记
        local mark
        [ "${SELECTED[$i]}" -eq 1 ] && mark="${GREEN}✔${NC}" || mark="${DIM}○${NC}"

        # 光标
        local cursor_mark=" "
        [ $i -eq $cursor ] && cursor_mark="${YELLOW}▶${NC}"

        # 端口信息
        local port_info=""
        [ -n "${SVC_PORTS[$i]}" ] && port_info="${DIM}[${SVC_PORTS[$i]}]${NC} "

        # 行
        local line="${cursor_mark} [${mark}] ${SVC_NAME[$i]}"
        printf "  %-30s %s%s\n" "$line" "$port_info" "${DIM}${SVC_DESC[$i]}${NC}"
    done

    echo ""
    local count=0
    for ((i=0; i<SERVICE_COUNT; i++)); do
        [ "${SELECTED[$i]}" -eq 1 ] && ((count++))
    done
    echo -e "  ${BOLD}已选: ${count} 项${NC}"
}

# 多选菜单主循环
multiselect() {
    local cursor=0

    while true; do
        draw_menu "$cursor"

        # 读取按键
        read -rsn1 key
        case "$key" in
            $'\x1b')
                # 方向键序列
                read -rsn1 -t 0.1 key2
                if [ "$key2" = "[" ]; then
                    read -rsn1 -t 0.1 key3
                    case "$key3" in
                        A) [ $cursor -gt 0 ] && ((cursor--)) ;;           # ↑
                        B) [ $cursor -lt $((SERVICE_COUNT-1)) ] && ((cursor++)) ;;  # ↓
                        C) ;;  # → (忽略)
                        D) ;;  # ← (忽略)
                    esac
                fi
                ;;
            ' ')
                # 空格: 切换选中
                SELECTED[$cursor]=$((1 - ${SELECTED[$cursor]}))
                ;;
            'a'|'A')
                # 全选
                for ((i=0; i<SERVICE_COUNT; i++)); do SELECTED[$i]=1; done
                ;;
            'n'|'N')
                # 全不选
                for ((i=0; i<SERVICE_COUNT; i++)); do SELECTED[$i]=0; done
                ;;
            '')
                # 回车: 确认
                local count=0
                for ((i=0; i<SERVICE_COUNT; i++)); do
                    [ "${SELECTED[$i]}" -eq 1 ] && ((count++))
                done
                if [ $count -eq 0 ]; then
                    echo -e "\n${YELLOW}未选择任何服务${NC}"
                    read -p "按回车继续..." _
                    continue
                fi
                return 0
                ;;
            'q'|'Q'|$'\x03')
                echo -e "\n${YELLOW}已退出${NC}"
                exit 0
                ;;
        esac
    done
}

# ============================================================
# 主流程
# ============================================================

main() {
    check_root
    ensure_tools

    # 检测系统信息
    echo ""
    log_step "系统信息"
    echo "  主机名:  $(hostname)"
    echo "  IP:      $(hostname -I 2>/dev/null | awk '{print $1}')"
    echo "  架构:    $(uname -m)"
    echo "  内存:    $(free -h | awk '/Mem/{print $2}')"
    echo "  内核:    $(uname -r)"
    echo "  数据目录: $DATA_DIR"

    # Docker 检测
    if has_cmd docker; then
        echo "  Docker:  $(docker --version 2>/dev/null | awk '{print $1,$2,$3}')"
    else
        echo -e "  Docker:  ${YELLOW}未安装${NC}"
    fi
    echo ""

    # 显示菜单
    multiselect

    # 收集选中的服务
    local -a selected_ids=()
    local -a selected_indices=()
    for ((i=0; i<SERVICE_COUNT; i++)); do
        if [ "${SELECTED[$i]}" -eq 1 ]; then
            selected_ids+=("${SVC_ID[$i]}")
            selected_indices+=($i)
        fi
    done

    # 创建数据目录
    mkdir -p "$DATA_DIR"

    # ---- 端口冲突检测 ----
    local has_conflict=false
    if check_port_conflicts "${selected_ids[@]}"; then
        : # 无冲突
    else
        has_conflict=true
    fi

    if [ "$has_conflict" = true ]; then
        echo ""
        log_warn "检测到端口冲突!"
        read -p "是否继续安装(冲突端口可能导致服务启动失败)? [y/N] " cont
        [[ "$cont" =~ ^[Yy]$ ]] || { log_info "已取消"; exit 0; }
    fi

    # ---- 确认安装 ----
    echo ""
    log_step "即将安装以下服务:"
    for idx in "${selected_indices[@]}"; do
        echo -e "  ${GREEN}✔${NC} ${SVC_NAME[$idx]} - ${SVC_DESC[$idx]}"
    done
    echo ""
    read -p "确认开始安装? [y/N] " confirm
    [[ "$confirm" =~ ^[Yy]$ ]] || { log_info "已取消"; exit 0; }

    # ---- 逐个安装 ----
    local success=0
    local failed=0
    local -a failed_list=()

    for idx in "${selected_indices[@]}"; do
        local id="${SVC_ID[$idx]}"
        local name="${SVC_NAME[$idx]}"
        local func="${SVC_FUNC[$idx]}"

        echo ""
        log_step "安装 ${name} ($((success+failed+1))/${#selected_indices[@]})"

        if $func; then
            log_success "${name} 安装完成"
            ((success++))
        else
            log_error "${name} 安装失败"
            ((failed++))
            failed_list+=("$name")
        fi
    done

    # ---- 安装总结 ----
    echo ""
    echo -e "${BOLD}${CYAN}"
    echo "╔══════════════════════════════════════════════════════════╗"
    echo "║                    安装完成                               ║"
    echo "╠══════════════════════════════════════════════════════════╣"
    echo -e "║  ${GREEN}成功: ${success}${CYAN}  ${RED}失败: ${failed}${CYAN}  总计: ${#selected_indices[@]}${NC}${CYAN}                   ║"
    echo "╚══════════════════════════════════════════════════════════╝"
    echo -e "${NC}"

    if [ $failed -gt 0 ]; then
        log_error "失败的服务:"
        for name in "${failed_list[@]}"; do
            echo "  - $name"
        done
    fi

    # 显示服务访问地址
    echo ""
    log_step "服务访问地址"
    local my_ip
    my_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    for idx in "${selected_indices[@]}"; do
        local ports="${SVC_PORTS[$idx]}"
        local name="${SVC_NAME[$idx]}"
        # 取第一个 TCP 端口
        local first_port=""
        if [ -n "$ports" ]; then
            local first_entry="${ports%%,*}"
            first_port="${first_entry%%/*}"
        fi
        if [ -n "$first_port" ]; then
            printf "  %-24s http://%s:%s\n" "$name" "$my_ip" "$first_port"
        else
            printf "  %-24s (无固定端口)\n" "$name"
        fi
    done

    echo ""
    log_info "数据目录: $DATA_DIR"
    log_info "配置文件在各服务子目录下"
    echo ""
}

main "$@"
