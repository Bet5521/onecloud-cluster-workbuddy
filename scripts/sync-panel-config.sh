#!/bin/bash
# ============================================================
# OneCloud Cluster - 面板配置同步 (sync-panel-config.sh)
#
# 解决的问题
#   「部署节点 / 面板时修改的节点 IP 等, 没有同步写入面板配置文件」:
#     - 节点 IP 的唯一数据源是 inventory/nodes.yaml (+ nodes.local.yaml / 环境变量);
#     - panel/config.json 由 scripts/gen-panel-config.sh 从清单生成;
#     - 但面板实际运行目录可能是稳定目录 (/opt/onecloud/panel), 仓库里那份不是它。
#   结果: 改了 IP 只更新了仓库副本, 面板读到的仍是旧值 -> 监控/操作失效。
#
# 本脚本做一件事: 把清单里的最新节点信息, **一次刷新到面板真正读取的每一处**。
#   1. 仓库内 panel/config.json   (保持源码副本新鲜)
#   2. 面板安装目录的 config.json (从 systemd unit 的 PANEL_CONFIG 解析, 权威)
#   3. ONECLOUD_PANEL_INSTALL_DIR / /opt/onecloud/panel (若存在)
#   再按需重启 onecloud-panel 使新配置生效。
#
# 用法
#   bash scripts/sync-panel-config.sh                 # 同步所有已知位置
#   bash scripts/sync-panel-config.sh --restart       # 同步后重启面板服务
#   bash scripts/sync-panel-config.sh --repo-only     # 只刷新仓库内副本
#   bash scripts/sync-panel-config.sh --dry-run       # 只显示将要写入的位置
#   bash scripts/sync-panel-config.sh --out DIR       # 额外指定一个目录
#
# 退出码: 0=成功; 1=生成失败或路径不可写
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

log_info()  { echo -e "\033[0;32m[INFO]\033[0m $*"; }
log_warn()  { echo -e "\033[1;33m[WARN]\033[0m $*"; }
log_error() { echo -e "\033[0;31m[ERROR]\033[0m $*"; }
log_ok()    { echo -e "\033[0;32m[ OK ]\033[0m $*"; }

DRY_RUN=0
DO_RESTART=0
REPO_ONLY=0
EXTRA_DIRS=()

while [ $# -gt 0 ]; do
    case "$1" in
        -n|--dry-run) DRY_RUN=1; shift ;;
        --restart)    DO_RESTART=1; shift ;;
        --repo-only)  REPO_ONLY=1; shift ;;
        --out)        EXTRA_DIRS+=("${2:-}"); shift 2 ;;
        -h|--help)
            sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) log_error "未知选项: $1"; exit 2 ;;
    esac
done

GEN="${SCRIPT_DIR}/gen-panel-config.sh"
[ -f "$GEN" ] || { log_error "缺失 ${GEN}"; exit 1; }

PANEL_UNIT="${ONECLOUD_PANEL_UNIT:-/etc/systemd/system/onecloud-panel.service}"
PANEL_SERVICE_NAME="${ONECLOUD_PANEL_SERVICE_NAME:-onecloud-panel}"

# ------------------------------------------------------------
# 收集目标 config.json 路径 (去重, 记录顺序)
# ------------------------------------------------------------
declare -a TARGETS=()
_add_target() {
    local p="$1" x
    [ -n "$p" ] || return 0
    for x in "${TARGETS[@]:-}"; do
        [ "$x" = "$p" ] && return 0
    done
    TARGETS+=("$p")
}

# 1) 仓库内副本 (永远刷新; 可用 ONECLOUD_PANEL_REPO_CONFIG 重定向, 便于测试)
_add_target "${ONECLOUD_PANEL_REPO_CONFIG:-${ROOT_DIR}/panel/config.json}"

if [ "$REPO_ONLY" != 1 ]; then
    # 2) 从 systemd unit (及其 drop-in) 解析面板真正读取的 PANEL_CONFIG
    _unit_cfg="$(grep -hosE 'PANEL_CONFIG=[^[:space:]]+' "$PANEL_UNIT" \
        "${PANEL_UNIT}.d"/*.conf 2>/dev/null | tail -1 | cut -d= -f2- || true)"
    _add_target "${_unit_cfg:-}"

    # 3) 显式安装目录
    if [ -n "${ONECLOUD_PANEL_INSTALL_DIR:-}" ]; then
        _add_target "${ONECLOUD_PANEL_INSTALL_DIR%/}/config.json"
    fi
    for d in "${EXTRA_DIRS[@]:-}"; do
        [ -n "$d" ] && _add_target "${d%/}/config.json"
    done

    # 4) 回退根下的面板目录 (若确实存在)
    _fallback="${INSTALL_FALLBACK_ROOT:-/opt/onecloud}/panel"
    [ -d "$_fallback" ] && _add_target "${_fallback}/config.json"
fi

echo ""
log_info "将同步面板配置到以下位置:"
for t in "${TARGETS[@]}"; do
    echo "    - ${t}"
done
echo ""

if [ "$DRY_RUN" = 1 ]; then
    log_info "--dry-run: 未写入任何文件"
    exit 0
fi

# ------------------------------------------------------------
# 逐个生成
# ------------------------------------------------------------
ok=0; fail=0
for t in "${TARGETS[@]}"; do
    d="$(dirname "$t")"
    if [ ! -d "$d" ]; then
        # 面板安装目录尚不存在 -> 跳过 (不是错误)
        log_warn "跳过 (目录不存在): ${d}"
        continue
    fi
    if bash "$GEN" --out "$t" >/dev/null 2>&1; then
        log_ok "已刷新: ${t}"
        ok=$((ok + 1))
    else
        log_error "生成失败: ${t}"
        fail=$((fail + 1))
    fi
done

if [ "$ok" -eq 0 ]; then
    log_error "没有任何位置被刷新 (面板可能尚未安装到稳定目录)"
    exit 1
fi

# ------------------------------------------------------------
# 按需重启面板服务
# ------------------------------------------------------------
if [ "$DO_RESTART" = 1 ]; then
    if command -v systemctl >/dev/null 2>&1; then
        if systemctl list-unit-files 2>/dev/null | grep -q "^${PANEL_SERVICE_NAME}\.service"; then
            _sudo=""
            [ "$(id -u 2>/dev/null || echo 0)" -ne 0 ] && _sudo="sudo"
            if $_sudo systemctl restart "$PANEL_SERVICE_NAME"; then
                log_ok "已重启 ${PANEL_SERVICE_NAME}"
            else
                log_warn "重启 ${PANEL_SERVICE_NAME} 失败 (可手动: systemctl restart ${PANEL_SERVICE_NAME})"
            fi
        else
            log_warn "未找到 ${PANEL_SERVICE_NAME}.service, 跳过重启"
        fi
    else
        log_warn "系统无 systemd, 跳过重启"
    fi
else
    log_info "提示: 若面板已在运行, 请重启以生效: systemctl restart ${PANEL_SERVICE_NAME}"
fi

[ "$fail" -eq 0 ]
