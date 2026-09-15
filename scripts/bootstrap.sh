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
        _cidr="$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4; exit}')"
        if [ -n "$_cidr" ]; then
            CUR_IP="${_cidr%%/*}"
            CUR_PREFIX="${_cidr##*/}"
        fi
        CUR_GW="$(ip route show default 2>/dev/null \
                  | awk '{for (i = 1; i <= NF; i++) if ($i == "via") {print $(i+1); exit}}')"
    fi
    # 兜底: net-tools 的 route
    if [ -z "$CUR_GW" ] && command -v route >/dev/null 2>&1; then
        CUR_GW="$(route -n 2>/dev/null | awk '$1 == "0.0.0.0" {print $2; exit}')"
    fi
    if [ -z "$CUR_IP" ] && command -v hostname >/dev/null 2>&1; then
        CUR_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
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
      --dry-run         只打印将要应用的配置, 不修改系统 (可单独使用)
      --no-detect       禁用本机网络探测 (不自动获取当前 IP/网关)
  -h, --help            显示帮助

未通过参数提供的选项, 会在终端可用时以交互方式询问。
已在 inventory/nodes.yaml 登记的节点, IP/主机名/网关/DNS 自动取清单值。

网络取值优先级:
  IP     命令行 --ip  >  本机探测(询问)  >  清单
  网关   命令行 --gateway  >  由最终 IP 自动推导  >  本机探测  >  清单

关键行为:
  * 指定 --ip 后, 会自动按同网段推导网关 (网络地址+1);
    与现网关不在同一网段时提示并询问是否调整, --yes 下自动调整。
  * 未指定 --ip 时, 先探测本机当前 IP/网关, 询问是否直接采用;
    --yes 下自动采用。用 --no-detect 可关闭该行为。

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
NO_DETECT=false
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|--node)      NODE_NAME="$2";    shift 2 ;;
        -i|--ip)        NODE_IP="$2";      shift 2 ;;
        -H|--hostname)  HOSTNAME="$2";     shift 2 ;;
        -g|--gateway)   GATEWAY="$2";      shift 2 ;;
        -D|--dns)       DNS_SERVERS="$2";  shift 2 ;;
        -s|--sd)        SD_DEV="$2";       shift 2 ;;
        -y|--yes)       ASSUME_YES=true;   shift ;;
        --dry-run)      DRY_RUN=true;      shift ;;
        --no-detect)    NO_DETECT=true;    shift ;;
        -h|--help)      usage; exit 0 ;;
        *) log_error "未知选项: $1"; usage; exit 1 ;;
    esac
done

# ---- 1a. 用清单填充未显式给出的参数 (清单只作为默认值, 命令行优先) ----
INVENTORY_HIT=false
IP_SOURCE="命令行"
GW_SOURCE="命令行"
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
if [ -z "$DNS_SERVERS" ]; then
    DNS_SERVERS="${ONECLOUD_DNS:-${NET_DNS:-}}"
fi

# ---- 1b. 探测本机当前网络 (IP / 前缀 / 网关) ----
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
DETECT_IP=""; DETECT_GW=""; DETECT_PREFIX=""
if [ "$NO_DETECT" != true ]; then
    detect_current_network
    DETECT_IP="$CUR_IP"; DETECT_GW="$CUR_GW"; DETECT_PREFIX="$CUR_PREFIX"
    if [ -n "$DETECT_IP" ] || [ -n "$DETECT_GW" ]; then
        log_info "探测到本机当前网络: IP=${DETECT_IP:-未获取}/${DETECT_PREFIX:-?} 网关=${DETECT_GW:-未获取}"
    fi
fi

# 前缀: 本机探测 > 清单 > 24
if [ -n "$DETECT_PREFIX" ]; then
    LAN_PREFIX="$DETECT_PREFIX"
else
    LAN_PREFIX="${NET_LAN_PREFIX:-24}"
fi

# ---- 1c. 未指定 IP 时: 询问是否直接采用本机当前 IP / 网关 ----
if [ -z "$NODE_IP" ] && [ -n "$DETECT_IP" ]; then
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

# 交互补全: 仅在有终端时询问仍未提供的参数
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
    if [ -z "$SD_DEV" ]; then
        read -r -p "请输入 SD 卡设备名 (如 mmcblk1): " SD_DEV || true
    fi
fi

# ---- 1d. 默认值兜底 ----
if [ -z "$NODE_NAME" ]; then
    NODE_NAME="wk-node-01"
fi
if [ -z "$HOSTNAME" ]; then
    HOSTNAME="${NODE_NAME#wk-}"
fi
if [ -z "$SD_DEV" ]; then
    SD_DEV="mmcblk1"
fi
if [ -z "$DNS_SERVERS" ]; then
    DNS_SERVERS="1.1.1.1"
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

if [ -z "$GATEWAY" ]; then
    log_error "未指定网关 (--gateway 或 ONECLOUD_GATEWAY 或清单 network.gateway)"
    exit 1
fi

# ---- 1f. 最终一致性检查: 网关与 IP 不同网段时明确告警 ----
FINAL_IP_NET="$(ip_net_addr "$NODE_IP" "$LAN_PREFIX" 2>/dev/null || true)"
FINAL_GW_NET="$(ip_net_addr "$GATEWAY" "$LAN_PREFIX" 2>/dev/null || true)"
if [ -n "$FINAL_IP_NET" ] && [ -n "$FINAL_GW_NET" ] && [ "$FINAL_IP_NET" != "$FINAL_GW_NET" ]; then
    log_warn "注意: 网关 ${GATEWAY} 与 IP ${NODE_IP} 不在同一网段 (/${LAN_PREFIX})"
    log_warn "      网关不可直达时, 配置静态 IP 后将无法联网; 确认无误可忽略"
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
echo "  静态IP:   $NODE_IP/${LAN_PREFIX}    (来源: ${IP_SOURCE})"
echo "  主机名:   $HOSTNAME"
echo "  网关:     $GATEWAY    (来源: ${GW_SOURCE})"
echo "  DNS:      $DNS_LIST"
echo "  SD设备:   /dev/${SD_DEV}"
if [ -n "$DETECT_IP" ] || [ -n "$DETECT_GW" ]; then
    echo "  本机现状: IP=${DETECT_IP:-未获取} 网关=${DETECT_GW:-未获取}"
fi
echo ""

# 干跑: 只展示将要写入的配置, 不触碰系统 (放在确认之前, 可单独使用)
if [ "$DRY_RUN" = true ]; then
    log_info "干跑模式: 以上为将要应用的配置, 未做任何修改"
    echo ""
    echo "  将写入: /etc/network/interfaces 或 /etc/netplan/99-static.yaml"
    echo "  将设置: hostname=${HOSTNAME}, address=${NODE_IP}/${LAN_PREFIX}, gateway=${GATEWAY}"
    echo ""
    exit 0
fi

if [ "$ASSUME_YES" = true ]; then
    CONFIRM="y"
elif [ "$INTERACTIVE" = true ]; then
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
