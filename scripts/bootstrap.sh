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
  -D, --dns IP          DNS (默认: 清单 network.dns, 多个用逗号分隔)
  -s, --sd DEV          SD 卡设备名 (如 mmcblk1, 不含 /dev/; 默认自动探测)
  -m, --sd-mount DIR    SD 卡挂载点 (默认 /mnt/sd)
      --no-sd           完全跳过 SD 卡挂载与 Docker 数据迁移
      --no-sd-automount 挂载 SD 卡但不写入 fstab (不自动挂载)
  -y, --yes             跳过交互确认 (非交互/自动化场景必填)
      --dry-run         只打印将要应用的配置, 不修改系统 (可单独使用)
      --no-detect       不自动采用本机探测到的 IP/网关 (仍会探测并用于风险提示)
  -h, --help            显示帮助

参数与询问的关系:
  传入的参数一律直接生效, 不会被询问覆盖;
  只有"未提供且无法从清单/探测推断"的项, 才在终端可用时交互询问。
  非交互环境 (cron/CI) 请配合 --yes, 否则缺失项会直接报错而不是干等输入。

网络取值优先级:
  IP     命令行 --ip  >  本机探测(询问/--yes 采用)  >  清单
  网关   命令行 --gateway  >  由最终 IP 推导  >  本机探测  >  清单

执行前的安全检查 (防止配完静态 IP 后失联):
  * 新 IP 与本机当前 IP 不同网段 -> 告警并要求确认
  * 新 IP 已被占用 (ping 有响应) -> 告警并要求确认
  * 网关 ping 不可达 -> 告警并要求确认
  --yes 下仅告警后继续; --dry-run 下只提示不改动。

环境变量 (优先级最高):
  ONECLOUD_WK_EDGE_01_IP / _HOSTNAME / _WG_IP
  ONECLOUD_GATEWAY / ONECLOUD_DNS / ONECLOUD_LAN_SUBNET / ONECLOUD_DOMAIN

示例:
  $0 --node wk-edge-01 --yes                                  # 全部取清单值
  $0 --node wk-edge-01 --ip 10.0.0.5 --hostname edge-01 --yes  # 覆盖 IP 与主机名
  $0 --node wk-new --ip 10.0.0.9 --hostname new --gateway 10.0.0.1 --yes
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

NODE_NAME=""
NODE_IP=""
HOSTNAME=""
SD_DEV=""
GATEWAY=""
DNS_SERVERS=""
SD_MOUNT="/mnt/sd"
ASSUME_YES=false
NO_DETECT=false
DRY_RUN=false
NO_SD=false
NO_SD_AUTOMOUNT=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|--node)      NODE_NAME="$2";    shift 2 ;;
        -i|--ip)        NODE_IP="$2";      shift 2 ;;
        -H|--hostname)  HOSTNAME="$2";     shift 2 ;;
        -g|--gateway)   GATEWAY="$2";      shift 2 ;;
        -D|--dns)       DNS_SERVERS="$2";  shift 2 ;;
        -s|--sd)        SD_DEV="$2";       shift 2 ;;
        -m|--sd-mount)  SD_MOUNT="$2";     shift 2 ;;
        --no-sd)        NO_SD=true;        shift ;;
        --no-sd-automount) NO_SD_AUTOMOUNT=true; shift ;;
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
if [ -z "$DNS_SERVERS" ]; then
    DNS_SERVERS="${ONECLOUD_DNS:-${NET_DNS:-}}"
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
    if [ -z "$DNS_SERVERS" ]; then
        read -r -p "请输入 DNS (默认 1.1.1.1, 多个用逗号分隔): " DNS_SERVERS || true
    fi
fi

# ---- 1d. 默认值兜底 ----
if [ -z "$NODE_NAME" ]; then
    NODE_NAME="wk-node-01"
fi
if [ -z "$HOSTNAME" ]; then
    HOSTNAME="${NODE_NAME#wk-}"
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
if [ "$SD_ENABLE" = true ]; then
    echo "  SD设备:   /dev/${SD_DEV}"
    echo "  挂载点:   ${SD_MOUNT}    (自动挂载: $([ "$SD_AUTOMOUNT" = true ] && echo 是 || echo 否))"
else
    echo "  SD设备:   未启用 (跳过挂载与 Docker 数据迁移)"
fi
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
grep -q '^net.ipv4.ip_forward=1' /etc/sysctl.conf 2>/dev/null || \
    echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf
grep -q '^net.ipv4.conf.all.src_valid_mark=1' /etc/sysctl.conf 2>/dev/null || \
    echo 'net.ipv4.conf.all.src_valid_mark=1' >> /etc/sysctl.conf
sysctl -p 2>/dev/null || true

# ---- 14. 创建目录结构 ----
DATA_ROOT="${SD_MOUNT}"
if [ "$SD_MOUNTED" != true ]; then
    DATA_ROOT="/mnt/sd"
fi
log_info "创建目录结构: ${DATA_ROOT}/srv/${NODE_NAME}"
mkdir -p "${DATA_ROOT}/srv/${NODE_NAME}"/{cloudflared,adguard/{work,conf},wireguard/config,
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
if [ "$SD_MOUNTED" = true ]; then
    echo "  存储:   ${SD_MOUNT} (SD卡, 自动挂载: $([ "$SD_AUTOMOUNT" = true ] && echo 是 || echo 否))"
else
    echo "  存储:   未挂载 SD 卡 (数据位于 eMMC ${DATA_ROOT})"
fi
echo "  Swap:   2GB"
echo ""
echo "下一步:"
echo "  1. 将此节点的 SSH 公钥添加到其他节点的 authorized_keys"
echo "  2. 克隆 onecloud-cluster 仓库到 ${SD_MOUNT}/"
echo "  3. 复制对应 node-xxx 目录的 docker-compose.yml 到 ${DATA_ROOT}/srv/${NODE_NAME}/"
echo "  4. 运行 ./scripts/deploy.sh 分发配置"
echo "  5. 启动服务: cd ${DATA_ROOT}/srv/${NODE_NAME} && docker-compose up -d"
echo ""
echo "SSH 公钥:"
cat /root/.ssh/id_ed25519.pub
echo ""
log_info "建议立即重启: reboot"
