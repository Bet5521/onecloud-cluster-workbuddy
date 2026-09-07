#!/bin/bash
# ============================================================
# 玩客云节点初始化脚本 (bootstrap.sh)
# 在新刷好 Armbian 的玩客云上运行一次
# ============================================================
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

usage() {
    cat << EOF
用法: $0 [选项]

选项:
  -n, --node NAME       节点名称 (如 wk-edge-01)
  -i, --ip IP           静态 IP (如 192.168.1.101)
  -H, --hostname NAME   主机名 (如 edge-01)
  -s, --sd DEV          SD 卡设备名 (如 mmcblk1, 不含 /dev/)
  -y, --yes             跳过交互确认 (非交互/自动化场景必填)
  -h, --help            显示帮助

未通过参数提供的选项, 会在终端可用时以交互方式询问。
非交互环境 (无 TTY) 下缺失的参数将使用默认值。

示例:
  $0 --node wk-edge-01 --ip 192.168.1.101 --hostname edge-01 --yes
EOF
}

echo ""
echo "=========================================="
echo "  OneCloud Cluster - Node Bootstrap"
echo "=========================================="
echo ""

# ---- 1. 基本信息采集 ----
NODE_NAME=""
NODE_IP=""
HOSTNAME=""
SD_DEV=""
ASSUME_YES=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|--node)      NODE_NAME="$2"; shift 2 ;;
        -i|--ip)        NODE_IP="$2";   shift 2 ;;
        -H|--hostname)  HOSTNAME="$2";  shift 2 ;;
        -s|--sd)        SD_DEV="$2";    shift 2 ;;
        -y|--yes)       ASSUME_YES=true; shift ;;
        -h|--help)      usage; exit 0 ;;
        *) log_error "未知选项: $1"; usage; exit 1 ;;
    esac
done

# 交互补全: 仅在有终端时询问未提供的参数
if [ -t 0 ]; then
    if [ -z "$NODE_NAME" ]; then read -r -p "请输入节点名称 (如 wk-edge-01): " NODE_NAME || true; fi
    if [ -z "$NODE_IP" ];   then read -r -p "请输入静态IP (如 192.168.1.101): " NODE_IP   || true; fi
    if [ -z "$HOSTNAME" ];  then read -r -p "请输入主机名 (如 edge-01): " HOSTNAME        || true; fi
    if [ -z "$SD_DEV" ];    then read -r -p "请输入 SD 卡设备名 (如 mmcblk1): " SD_DEV   || true; fi
fi

[ -z "$NODE_NAME" ] && NODE_NAME="wk-node-01"
[ -z "$NODE_IP" ]   && NODE_IP="192.168.1.101"
[ -z "$HOSTNAME" ]  && HOSTNAME="${NODE_NAME#wk-}"
[ -z "$SD_DEV" ]    && SD_DEV="mmcblk1"

echo ""
log_info "配置信息:"
echo "  节点名称: $NODE_NAME"
echo "  静态IP:   $NODE_IP"
echo "  主机名:   $HOSTNAME"
echo "  SD设备:   /dev/${SD_DEV}"
echo ""

if [ "$ASSUME_YES" = true ]; then
    CONFIRM="y"
elif [ -t 0 ]; then
    read -r -p "确认无误? [y/N] " CONFIRM || true
else
    log_warn "非交互环境且未指定 --yes, 已取消"
    exit 0
fi
[[ "$CONFIRM" =~ ^[Yy]$ ]] || { log_warn "已取消"; exit 0; }

# ---- 2. 设置主机名 ----
log_info "设置主机名: $HOSTNAME"
hostnamectl set-hostname "$HOSTNAME"
echo "$HOSTNAME" > /etc/hostname

# ---- 3. 换国内源 ----
log_info "配置国内 apt 源..."
cat > /etc/apt/sources.list << 'EOF'
deb https://mirrors.tuna.tsinghua.edu.cn/debian/ bullseye main contrib non-free
deb https://mirrors.tuna.tsinghua.edu.cn/debian/ bullseye-updates main contrib non-free
deb https://mirrors.tuna.tsinghua.edu.cn/debian/ bullseye-backports main contrib non-free
deb https://mirrors.tuna.tsinghua.edu.cn/debian-security bullseye-security main contrib non-free
EOF

# ---- 4. 更新系统 ----
log_info "更新系统包..."
apt update
apt upgrade -y

# ---- 5. 安装基础工具 ----
log_info "安装基础工具..."
apt install -y curl wget git vim htop iotop net-tools dnsutils \
    parted fdisk dosfstools rsync unzip jq ca-certificates \
    gnupg lsb-release software-properties-common \
    wireguard-tools wireguard-dkms

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

# ---- 8. SD 卡分区和挂载 ----
SD_PATH="/dev/${SD_DEV}"
if [ -b "$SD_PATH" ]; then
    SD_PART="${SD_PATH}p1"
    if ! grep -q "/mnt/sd" /etc/fstab 2>/dev/null; then
        log_info "挂载 SD 卡 ${SD_PART}..."
        mkdir -p /mnt/sd
        if ! mountpoint -q /mnt/sd; then
            mount "${SD_PART}" /mnt/sd 2>/dev/null || {
                log_warn "SD 卡可能未格式化, 尝试创建分区..."
                parted -s "$SD_PATH" mklabel gpt mkpart primary ext4 1MiB 100%
                sleep 2
                mkfs.ext4 -F "${SD_PART}"
                mount "${SD_PART}" /mnt/sd
            }
        fi
        echo "${SD_PART} /mnt/sd ext4 defaults,noatime 0 2" >> /etc/fstab
    fi
else
    log_error "未找到 SD 卡设备 ${SD_PATH}, 跳过挂载"
fi

# ---- 9. 迁移 Docker 数据到 SD 卡 ----
log_info "迁移 Docker 数据到 SD 卡..."
mkdir -p /mnt/sd/docker /mnt/sd/srv /mnt/sd/backups

if ! grep -q "DOCKER_OPTS" /etc/default/docker 2>/dev/null; then
    mkdir -p /mnt/sd/docker
    if [ -d /var/lib/docker ] && [ "$(ls -A /var/lib/docker 2>/dev/null)" ]; then
        systemctl stop docker 2>/dev/null || true
        rsync -avhP /var/lib/docker/ /mnt/sd/docker/ || true
    fi
    echo 'DOCKER_OPTS="-g /mnt/sd/docker --log-driver=json-file --log-opt max-size=5m --log-opt max-file=2"' >> /etc/default/docker
    mkdir -p /etc/docker
    cat > /etc/docker/daemon.json << 'EOF'
{
  "data-root": "/mnt/sd/docker",
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
    log_info "Docker 已迁移到 /mnt/sd/docker"
fi

# ---- 10. 固定 Docker 版本 ----
if dpkg -l | grep -q docker.io; then
    log_info "锁定 Docker 版本 (防止升级到 v29+)..."
    apt-mark hold docker.io
fi

# ---- 11. 设置静态 IP ----
log_info "配置静态 IP: $NODE_IP"
if [ -f /etc/network/interfaces ]; then
    cat > /etc/network/interfaces << EOF
auto lo
iface lo inet loopback

auto eth0
iface eth0 inet static
    address ${NODE_IP}/24
    gateway 192.168.1.1
    dns-nameservers 192.168.1.101 1.1.1.1
EOF
elif [ -d /etc/netplan ]; then
    cat > /etc/netplan/99-static.yaml << EOF
network:
  version: 2
  ethernets:
    eth0:
      addresses:
        - ${NODE_IP}/24
      routes:
        - to: default
          via: 192.168.1.1
      nameservers:
        addresses: [192.168.1.101, 1.1.1.1]
EOF
    netplan apply 2>/dev/null || true
fi

# ---- 12. 配置 /etc/hosts (幂等: 避免重复追加) ----
log_info "配置 hosts..."
while read -r host_ip host_alias; do
    for alias in $host_alias; do
        if ! grep -qE "[[:space:]]${alias}([[:space:]]|$)" /etc/hosts 2>/dev/null; then
            echo "${host_ip}  ${alias}" >> /etc/hosts
        fi
    done
done << EOF
192.168.1.101  wk-edge-01 edge-01.lan
192.168.1.102  wk-iot-02 iot-02.lan
192.168.1.103  wk-storage-03 storage-03.lan
192.168.1.104  wk-backup-04 backup-04.lan
EOF

# ---- 13. 启用 IP 转发 ----
log_info "启用 IP 转发..."
echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf
echo 'net.ipv4.conf.all.src_valid_mark=1' >> /etc/sysctl.conf
sysctl -p 2>/dev/null || true

# ---- 14. 创建目录结构 ----
log_info "创建目录结构..."
mkdir -p /mnt/sd/srv/${NODE_NAME}/{cloudflared,adguard/{work,conf},wireguard/config,
    clash,memos/data,homeassistant,piwigo/{config,gallery},xiaomusic,
    migpt,syncthing/{config,data},verysync/{temp},aria2/{config,downloads},
    cupsd/{config,printers,spool},cups-web/config,panel}

# ---- 15. 生成 SSH 密钥 (如不存在) ----
if [ ! -f /root/.ssh/id_ed25519 ]; then
    log_info "生成 SSH 密钥..."
    ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519 -C "root@${HOSTNAME}"
    chmod 600 /root/.ssh/id_ed25519
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
echo "  存储:   /mnt/sd (SD卡)"
echo "  Swap:   2GB"
echo ""
echo "下一步:"
echo "  1. 将此节点的 SSH 公钥添加到其他节点的 authorized_keys"
echo "  2. 克隆 onecloud-cluster 仓库到 /mnt/sd/"
echo "  3. 复制对应 node-xxx 目录的 docker-compose.yml 到 /mnt/sd/srv/${NODE_NAME}/"
echo "  4. 运行 ./scripts/deploy.sh 分发配置"
echo "  5. 启动服务: cd /mnt/sd/srv/${NODE_NAME} && docker-compose up -d"
echo ""
echo "SSH 公钥:"
cat /root/.ssh/id_ed25519.pub
echo ""
log_info "建议立即重启: reboot"
