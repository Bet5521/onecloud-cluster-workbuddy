#!/bin/bash
# ============================================================
# 节点 .env 生成器 (gen-node-env.sh)
#
# 从 inventory/nodes.yaml (+ 覆盖层) 渲染每个节点的 .env,
# 使自定义的 IP / 主机名 / WG 地址 / 域名真正传递到容器运行时。
#
# 行为:
#   - 模板: node-<节点名>/.env.example (入库)
#   - 产物: node-<节点名>/.env        (不入库, 已 gitignore)
#   - 托管键: NODE_NAME / NODE_IP / WG_IP / DOMAIN —— 始终以清单为准
#   - 其余键 (密钥/端口等): 已存在的 .env 中用户填过的值原样保留,
#     仅在缺失时回落到模板值
#
# 用法:
#   ./scripts/gen-node-env.sh              # 渲染所有节点
#   ./scripts/gen-node-env.sh wk-edge-01   # 仅渲染指定节点
#   ./scripts/gen-node-env.sh --dry-run    # 只打印将要写入的内容
# ============================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib-nodes.sh"

DRY_RUN=false
ONLY=""
for arg in "$@"; do
    case "$arg" in
        -n|--dry-run) DRY_RUN=true ;;
        -h|--help)
            sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) ONLY="$arg" ;;
    esac
done

# 托管键 —— 这些键始终以清单为准
MANAGED_KEYS="NODE_NAME NODE_IP WG_IP DOMAIN"

# 渲染单个节点: $1=节点名
render_node() {
    local node=$1
    local nodedir="${PROJECT_DIR}/node-${node}"
    local tmpl="${nodedir}/.env.example"
    local out="${nodedir}/.env"

    if [ ! -f "$tmpl" ]; then
        log_warn "跳过 ${node}: 无 .env.example 模板"
        return 0
    fi

    # 清单中没有有效值时保持模板原值, 避免写出空变量
    local ip wg domain kv
    ip="$(node_ip "$node")"
    wg="$(node_wg_ip "$node")"
    domain="$NET_DOMAIN"
    [ -z "$ip" ] && ip="$(grep -E '^NODE_IP=' "$tmpl" | head -1 | cut -d= -f2-)"
    [ -z "$wg" ] && wg="$(grep -E '^WG_IP=' "$tmpl" | head -1 | cut -d= -f2-)"
    [ -z "$domain" ] && domain="$(grep -E '^DOMAIN=' "$tmpl" | head -1 | cut -d= -f2-)"

    kv="NODE_NAME=${node};NODE_IP=${ip};WG_IP=${wg};DOMAIN=${domain}"

    # 已有 .env: 保留用户自定义值, 仅覆盖托管键
    # 无 .env  : 以模板为基础渲染
    local base="$tmpl"
    [ -f "$out" ] && base="$out"

    local rendered
    rendered=$(awk -v kv="$kv" '
    BEGIN {
        n = split(kv, A, ";")
        for (i = 1; i <= n; i++) {
            p = index(A[i], "=")
            K[i] = substr(A[i], 1, p - 1)
            V[K[i]] = substr(A[i], p + 1)
            SK[K[i]] = 0
        }
    }
    {
        line = $0
        sub(/\r$/, "", line)
        p = index(line, "=")
        k = (p > 0) ? substr(line, 1, p - 1) : ""
        if ((k in V) && k != "") {
            print k "=" V[k]
            SK[k] = 1
        } else {
            print line
        }
    }
    END {
        for (i = 1; i <= n; i++) {
            k = K[i]
            if (SK[k] == 0) print k "=" V[k]
        }
    }' "$base")

    if [ "$DRY_RUN" = true ]; then
        echo "----- ${out} -----"
        echo "$rendered"
        return 0
    fi

    printf '%s\n' "$rendered" > "$out"
    log_info "${node}: NODE_NAME=${node} NODE_IP=${ip} WG_IP=${wg} DOMAIN=${domain}"
}

if [ -n "$ONLY" ]; then
    if ! ONLY="$(node_resolve "$ONLY")"; then
        log_error "未知节点: ${ONLY} (可用: $(node_names | tr '\n' ' '))"
        exit 1
    fi
    render_node "$ONLY"
else
    for n in $(node_names); do
        render_node "$n"
    done
fi

[ "$DRY_RUN" = true ] || log_info "完成: .env 已渲染 (该文件不入库)"
