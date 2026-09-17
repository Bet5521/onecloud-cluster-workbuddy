#!/bin/bash
# ============================================================
# 配置分发脚本 (deploy.sh)
# 将项目配置 rsync 到各节点
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# 节点清单统一从 inventory 读取 (支持 nodes.local.yaml / 环境变量自定义)
# shellcheck source=lib-nodes.sh
source "${SCRIPT_DIR}/lib-nodes.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# 节点列表由 lib-nodes.sh 从 inventory/nodes.yaml 加载 (ALL_NODES)
# 格式: 节点名|主机名|IP|WG_IP|角色
require_nodes

usage() {
    cat << EOF
用法: $0 [选项]

选项:
  -n, --node NAME    指定节点名称 (如 wk-edge-01), 可多次指定
  -a, --all          分发到所有节点 (默认)
  -t, --test         仅测试连接, 不分发
  -e, --exec CMD     分发完成后在节点上远程执行命令
  -d, --dry-run      显示将要传输的内容, 不实际传输
  -h, --help         显示帮助

示例:
  $0                      # 分发所有节点
  $0 -n wk-edge-01        # 仅分发到边缘网关
  $0 -t                   # 测试所有节点 SSH 连接
  $0 -d                   # 预览将传输的文件
  $0 --exec "docker-compose up -d"   # 分发后启动所有服务
EOF
}

DRY_RUN=""
TEST_ONLY=false
EXEC_CMD=""
TARGET_NODES=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|--node)  TARGET_NODES+=("$2"); shift 2 ;;
        -a|--all)   TARGET_NODES=(); shift ;;
        -t|--test)  TEST_ONLY=true; shift ;;
        -e|--exec)  EXEC_CMD="$2"; shift 2 ;;
        -d|--dry-run) DRY_RUN="--dry-run"; shift ;;
        -h|--help)  usage; exit 0 ;;
        *) log_error "未知选项: $1"; usage; exit 1 ;;
    esac
done

echo ""
echo "=========================================="
echo "  OneCloud Cluster - Config Deploy"
echo "=========================================="
echo ""

deploy_node() {
    local NODE_NAME=$1
    local NODE_IP=$2
    local NODE_HOSTNAME=$3
    local NODE_SRC="${PROJECT_DIR}/node-${NODE_NAME}"
    # 远程数据根: 读取节点 /etc/onecloud/install.conf 的实际值 (SD 挂载点或 /opt 回退),
    # 取不到才回退 /mnt/sd。避免「无 SD 卡节点」被按 /mnt/sd 分发到不存在的路径。
    local REMOTE_ROOT REMOTE_BASE
    REMOTE_ROOT="$(node_data_root "$NODE_IP")"
    REMOTE_BASE="${REMOTE_ROOT}/srv/${NODE_NAME}"

    echo "--- $NODE_NAME ($NODE_IP, $NODE_HOSTNAME) ---"

    # 测试 SSH 连接
    if ! ssh -o ConnectTimeout=5 "root@${NODE_IP}" "echo ok" &>/dev/null; then
        log_error "无法连接到 $NODE_NAME ($NODE_IP)"
        return 1
    fi
    log_info "SSH 连接成功: $NODE_NAME"

    if [ "$TEST_ONLY" = true ]; then
        return 0
    fi

    # 确保远程目录存在
    ssh "root@${NODE_IP}" "mkdir -p ${REMOTE_BASE}/{cloudflared,adguard/{work,conf},wireguard/config,clash,memos/data,homeassistant,piwigo/{config,gallery},xiaomusic,migpt,typecho/usr,syncthing/{config,data},verysync,aria2/{config,downloads},cupsd/{config,printers,spool},cups-web/config,panel,gitea}"

    # 分发配置文件
    if [ -d "$NODE_SRC" ]; then
        log_info "分发 ${NODE_SRC} → ${NODE_IP}:${REMOTE_BASE}"
        rsync -avz $DRY_RUN \
            --exclude '*.bak' --exclude '*.old' \
            --exclude '.git' --exclude '__pycache__' \
            "${NODE_SRC}/" "root@${NODE_IP}:${REMOTE_BASE}/"
    else
        log_warn "本地未找到 $NODE_SRC, 跳过"
    fi

    # 分发脚本和文档到公共位置
    if [ "$DRY_RUN" != "--dry-run" ]; then
        ssh "root@${NODE_IP}" "mkdir -p ${REMOTE_ROOT}/scripts ${REMOTE_ROOT}/docs ${REMOTE_ROOT}/inventory"
        rsync -avz $DRY_RUN "${SCRIPT_DIR}/" "root@${NODE_IP}:${REMOTE_ROOT}/scripts/"
        rsync -avz $DRY_RUN "${PROJECT_DIR}/docs/" "root@${NODE_IP}:${REMOTE_ROOT}/docs/"
        rsync -avz $DRY_RUN "${PROJECT_DIR}/inventory/" "root@${NODE_IP}:${REMOTE_ROOT}/inventory/"
    fi

    # 远程执行命令 (--exec), 在节点服务目录下运行
    if [ -n "$EXEC_CMD" ] && [ "$DRY_RUN" != "--dry-run" ]; then
        log_info "在 $NODE_NAME 执行: $EXEC_CMD"
        if ssh "root@${NODE_IP}" "cd ${REMOTE_BASE} 2>/dev/null && ${EXEC_CMD}"; then
            log_info "$NODE_NAME 命令执行成功"
        else
            log_error "$NODE_NAME 命令执行失败: $EXEC_CMD"
            return 1
        fi
    fi

    log_info "$NODE_NAME 完成 ✓"
    echo ""
}

# 判断节点是否被 -n/--node 选中 (未指定则全部选中)
# 支持标准名 / 短名 / 主机名三种写法
node_selected() {
    local name=$1
    if [ ${#TARGET_NODES[@]} -eq 0 ]; then
        return 0
    fi
    local t resolved
    for t in "${TARGET_NODES[@]}"; do
        resolved=$(node_resolve "$t" 2>/dev/null) || continue
        [ "$resolved" = "$name" ] && return 0
    done
    return 1
}

for NODE in "${ALL_NODES[@]}"; do
    IFS='|' read -r NAME HOSTNAME IP WG_IP ROLE <<< "$NODE"
    node_selected "$NAME" || continue
    deploy_node "$NAME" "$IP" "$HOSTNAME" || true
done

echo "=========================================="
log_info "分发完成"
echo "=========================================="

# ------------------------------------------------------------
# 节点部署完毕 -> 生成防火墙设置建议清单
#
# onecloud 的部署脚本**不改任何节点的防火墙**: 不写 iptables / ip6tables,
# 不下发 ufw / firewall-cmd / nft, 也不生成任何 PostUp 规则。
# 这里只在控制端**静态**算出一份「该放行哪些端口」的建议清单
# (docs/firewall/<节点>.txt), 由你带着清单到节点上执行 setup_firewall.sh。
#
# 清单生成失败不影响分发结果 (节点已经部署好了), 因此不中断、只告警。
# ------------------------------------------------------------
if [ "$DRY_RUN" != "--dry-run" ] && [ "$TEST_ONLY" != true ]; then
    echo ""
    if [ -f "${SCRIPT_DIR}/firewall-recommend.sh" ]; then
        log_info "生成防火墙设置建议清单 (不改防火墙)"
        if bash "${SCRIPT_DIR}/firewall-recommend.sh"; then
            log_info "清单已生成 → docs/firewall/ , 由你执行 setup_firewall.sh 逐条应用"
        else
            log_warn "建议清单生成失败 (不影响节点分发)"
        fi
    else
        log_warn "未找到 scripts/firewall-recommend.sh, 跳过防火墙建议清单"
    fi
fi
