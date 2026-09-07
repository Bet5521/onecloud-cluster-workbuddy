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
  -i, --ip IP           静态 IP (清单中已登记的节点可省略)
  -H, --hostname NAME   主机名 (默认: 清单值, 或节点名去掉 wk- 前缀)
  -g, --gateway IP      网关 (默认: 清单 network.gateway)
  -D, --dns IP          DNS (默认: 清单 network.dns, 多个用逗号分隔)
  -s, --sd DEV          SD 卡设备名 (如 mmcblk1, 不含 /dev/)
  -y, --yes             跳过交互确认 (非交互/自动化场景必填)
  -h, --help            显示帮助

未通过参数提供的选项, 会在终端可用时以交互方式询问。
已在 inventory/nodes.yaml 登记的节点, IP/主机名/网关/DNS 自动取清单值。

环境变量 (优先级最高):
  ONECLOUD_WK_EDGE_01_IP / _HOSTNAME / _WG_IP
  ONECLOUD_GATEWAY / ONECLOUD_DNS / ONECLOUD_LAN_SUBNET / ONECLOUD_DOMAIN

示例:
  $0 --node wk-edge-01 --yes                                  # 全部取清单值
  $0 --node wk-edge-01 --ip 10.0.0.5 --hostname edge-01 --yes  # 覆盖 IP 与主机名
  $0 --node wk-new --ip 10.0.0.9 --hostname new --gateway 10.0.0.1 --yes
EOF
}

echo ""
echo "=========================================="
echo "  OneCloud Cluster - Node Bootstrap"
echo "=========================================="
echo ""

NODE_NAME=""
NODE_IP=""
HOSTNAME=""
SD_DEV=""
GATEWAY=""
DNS_SERVERS=""
ASSUME_YES=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|--node)      NODE_NAME="$2";    shift 2 ;;
        -i|--ip)        NODE_IP="$2";      shift 2 ;;
        -H|--hostname)  HOSTNAME="$2";     shift 2 ;;
        -g|--gateway)   GATEWAY="$2";      shift 2 ;;
        -D|--dns)       DNS_SERVERS="$2";  shift 2 ;;
        -s|--sd)        SD_DEV="$2";       shift 2 ;;
        -y|--yes)       ASSUME_YES=true;   shift ;;
        -h|--help)      usage; exit 0 ;;
        *) log_error "未知选项: $1"; usage; exit 1 ;;
    esac
done

# ---- 1a. 用清单填充未显式给出的参数 (清单只作为默认值, 命令行优先) ----
INVENTORY_HIT=false
if [ "$HAVE_INVENTORY" = true ] && [ -n "$NODE_NAME" ]; then
    if RESOLVED="$(node_resolve "$NODE_NAME" 2>/dev/null)"; then
        INVENTORY_HIT=true
        NODE_NAME="$RESOLVED"
        [ -z "$NODE_IP"  ] && NODE_IP="$(node_ip "$RESOLVED")"
        [ -z "$HOSTNAME" ] && HOSTNAME="$(node_hostname "$RESOLVED")"
    fi
fi

# 网络参数: 命令行 > 环境变量 > 清单
[ -z "$GATEWAY" ] && GATEWAY="${ONECLOUD_GATEWAY:-${NET_GATEWAY:-}}"
[ -z "$DNS_SERVERS" ] && DNS_SERVERS="${ONECLOUD_DNS:-${NET_DNS:-}}"
LAN_PREFIX="${NET_LAN_PREFIX:-24}"

# 交互补全: 仅在有终端时询问未提供的参数
if [ -t 0 ]; then
    if [ -z "$NODE_NAME" ]; then
        read -r -p "请输入节点名称 (如 wk-edge-01): " NODE_NAME || true
    fi
    if [ -z "$NODE_IP" ]; then
        read -r -p "请输入静态IP (如 ${NET_GATEWAY%.*}.101): " NODE_IP || true
    fi
    if [ -z "$HOSTNAME" ]; then
        read -r -p "请输入主机名 (如 edge-01): " HOSTNAME || true
    fi
    if [ -z "$GATEWAY" ]; then
        read -r -p "请输入网关 (如 192.168.1.1): " GATEWAY || true
    fi
    if [ -z "$SD_DEV" ]; then
        read -r -p "请输入 SD 卡设备名 (如 mmcblk1): " SD_DEV || true
    fi
fi

# ---- 1b. 默认值兜底 ----
[ -z "$NODE_NAME" ] && NODE_NAME="wk-node-01"
[ -z "$HOSTNAME" ]  && HOSTNAME="${NODE_NAME#wk-}"
[ -z "$SD_DEV" ]    && SD_DEV="mmcblk1"
[ -z "$DNS_SERVERS" ] && DNS_SERVERS="1.1.1.1"

# IP 与网关无法安全猜测: 缺失时明确报错, 而不是套用写死的网段
if [ -z "$NODE_IP" ]; then
    log_error "未指定节点 IP, 且清单中没有节点 '$NODE_NAME' 的记录"
    echo ""
    echo "请任选一种方式:"
    echo "  1) 命令行指定:  $0 --node $NODE_NAME --ip <你的IP> --yes"
    echo "  2) 环境变量:    ONECLOUD_$(echo "$NODE_NAME" | tr '[:lower:]-.' '[:upper:]__')_IP=<你的IP> $0 --node $NODE_NAME --yes"
    echo "  3) 登记到清单:  在 inventory/nodes.yaml 或 nodes.local.yaml 中添加该节点"
    exit 1
fi
if [ -z "$GATEWAY" ]; then
    log_error "未指定网关 (--gateway 或 ONECLOUD_GATEWAY 或清单 network.gateway)"
    exit 1
fi

# DNS 支持逗号分隔多个
DNS_LIST=""
IFS=',' read -ra _dns_arr <<< "$DNS_SERVERS"
for d in "${_dns_arr[@]}"; do
    d="$(echo "$d" | tr -d ' ')"
    [ -n "$d" ] && DNS_LIST="${DNS_LIST:+$DNS_LIST, }$d"
done

echo ""
log_info "配置信息:"
echo "  节点名称: $NODE_NAME"
echo "  静态IP:   $NODE_IP/${LAN_PREFIX}"
echo "  主机名:   $HOSTNAME"
echo "  网关:     $GATEWAY"
echo "  DNS:      $DNS_LIST"
echo "  SD设备:   /dev/${SD_DEV}"
[ "$INVENTORY_HIT" = true ] && echo "  (IP/主机名来自 inventory 清单)"
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
log_info "配置静态 IP: $NODE_IP/${LAN_PREFIX} (网关 $GATEWAY)"
if [ -f /etc/network/interfaces ]; then
    cat > /etc/network/interfaces << EOF
auto lo
iface lo inet loopback

auto eth0
iface eth0 inet static
    address ${NODE_IP}/${LAN_PREFIX}
    gateway ${GATEWAY}
    dns-nameservers ${DNS_LIST}
EOF
elif [ -d /etc/netplan ]; then
    # netplan 的 nameservers 需要 YAML 列表形式
    DNS_YAML=""
    IFS=',' read -ra _dns_arr2 <<< "$DNS_SERVERS"
    for d in "${_dns_arr2[@]}"; do
        d="$(echo "$d" | tr -d ' ')"
        [ -n "$d" ] && DNS_YAML="${DNS_YAML}        - ${d}
"
    done
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
    } > /etc/netplan/99-static.yaml
    netplan apply 2>/dev/null || true
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
