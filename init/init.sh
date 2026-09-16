#!/bin/bash
# ============================================================
# OneCloud 集群 · 交互式初始化入口
#   init/init.sh
#
# 覆盖能力:
#   1) 部署 Panel 控制面板 (systemd 常驻 / 前台试运行 / 仅装依赖)
#   2) 部署节点     (本机 bootstrap / 打开远程节点交互入口)
#   3) 节点维护     (健康巡检 / 备份 / 恢复 / 批量更新)
#   4) 配置与分发   (deploy / 远程执行 / WireGuard / 配置生成)
#   5) 服务安装     (统一安装 / 原生服务 / 节点容器)
#   6) 环境自检
#
# 设计约束 (重要):
#   - 本脚本不接受任何命令行参数, 传入参数直接报错退出
#   - 调用任何子脚本前先交互式询问, 由用户当场选择;
#     不预置 --yes / --ip 之类的默认参数
#   - 所有提问都要求显式输入 (y/n 或具体值), 不提供"回车即采用"的隐式默认值
#
# 用法:
#   bash init/init.sh
# ============================================================
set -u

# ------------------------------------------------------------
# 0. 拒绝命令行参数 (纯交互式)
# ------------------------------------------------------------
if [ "$#" -gt 0 ]; then
    printf '[ERROR] 本脚本是纯交互式入口, 不接受任何命令行参数。\n' >&2
    printf '        收到的参数: %s\n' "$*" >&2
    printf '        请直接运行: bash init/init.sh\n' >&2
    exit 2
fi

# ------------------------------------------------------------
# 1. 路径
# ------------------------------------------------------------
INIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$INIT_DIR")"
SCRIPTS_DIR="${PROJECT_ROOT}/scripts"
PANEL_DIR="${PROJECT_ROOT}/panel"
INVENTORY_DIR="${PROJECT_ROOT}/inventory"

# ------------------------------------------------------------
# 2. 节点清单库 (提供 node_* 查询函数)
# ------------------------------------------------------------
HAVE_INVENTORY=0
if [ -f "${SCRIPTS_DIR}/lib-nodes.sh" ]; then
    # shellcheck source=../scripts/lib-nodes.sh
    source "${SCRIPTS_DIR}/lib-nodes.sh" && HAVE_INVENTORY=1
fi

# 清单不可用时的退化实现, 保证菜单仍可打开
if [ "$HAVE_INVENTORY" != "1" ]; then
    node_names()    { return 0; }
    node_ip()       { echo "-"; }
    node_hostname() { echo "-"; }
    node_role()     { echo "-"; }
    node_wg_ip()    { echo "-"; }
    node_resolve()  { return 1; }
    load_nodes()    { return 0; }
fi

# ------------------------------------------------------------
# 2b. Python 依赖库 (pip 缺失时的多路降级)
#     复用 scripts/lib-pydeps.sh, 不在此重复实现
# ------------------------------------------------------------
if [ -f "${SCRIPTS_DIR}/lib-pydeps.sh" ]; then
    # shellcheck source=../scripts/lib-pydeps.sh
    source "${SCRIPTS_DIR}/lib-pydeps.sh"
fi

# ------------------------------------------------------------
# 2c. 面板监听地址工具 (探测本机网卡 / 校验监听地址)
#     复用 scripts/lib-panel-host.sh, 不在此重复实现
# ------------------------------------------------------------
if [ -f "${SCRIPTS_DIR}/lib-panel-host.sh" ]; then
    # shellcheck source=../scripts/lib-panel-host.sh
    source "${SCRIPTS_DIR}/lib-panel-host.sh"
fi

# ------------------------------------------------------------
#     复用 scripts/lib-network-audit.sh (通路/防火墙/SSH 通道自检)
#     只读探测, 供网络相关操作前把关; 缺失时相关提醒自动降级为跳过
# ------------------------------------------------------------
if [ -f "${SCRIPTS_DIR}/lib-network-audit.sh" ]; then
    # shellcheck source=../scripts/lib-network-audit.sh
    source "${SCRIPTS_DIR}/lib-network-audit.sh"
fi

# ------------------------------------------------------------
# 3. 颜色与日志 (在 source lib-nodes.sh 之后定义, 覆盖同名函数)
# ------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_ok()   { echo -e "${GREEN}${BOLD}[ OK ]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_err()  { echo -e "${RED}[ERROR]${NC} $*"; }
log_step() { echo -e "\n${BLUE}${BOLD}==> $*${NC}"; }

# root 时不再需要 sudo
SUDO=""
if [ "$(id -u 2>/dev/null || echo 0)" -ne 0 ]; then
    SUDO="sudo"
fi

# ------------------------------------------------------------
# 4. 交互基础工具
# ------------------------------------------------------------

# 分隔标题
header() {
    echo ""
    echo -e "${CYAN}${BOLD}══════════════════════════════════════════════════════════${NC}"
    echo -e "${CYAN}${BOLD}  $*${NC}"
    echo -e "${CYAN}${BOLD}══════════════════════════════════════════════════════════${NC}"
}

# 单选菜单: 结果写入 MENU_CHOICE
menu() {
    MENU_CHOICE=""
    local prompt="$1"; shift
    local -a opts=("$@")
    local n=${#opts[@]}
    local i=1 o ans

    echo ""
    for o in "${opts[@]}"; do
        printf '  %2d) %s\n' "$i" "$o"
        i=$((i + 1))
    done
    echo ""

    while :; do
        printf '%s [1-%d]: ' "$prompt" "$n"
        if ! IFS= read -r ans; then
            echo ""
            echo -e "${YELLOW}输入结束, 退出${NC}"
            exit 0
        fi
        case "$ans" in
            ''|*[!0-9]*)
                echo "  请输入 1-${n} 之间的编号"
                ;;
            *)
                if [ "$ans" -ge 1 ] && [ "$ans" -le "$n" ]; then
                    MENU_CHOICE="$ans"
                    return 0
                fi
                echo "  请输入 1-${n} 之间的编号"
                ;;
        esac
    done
}

# 读取一行必填输入, 结果打印到 stdout (提示语走 stderr)
read_input() {
    local prompt="$1" allow_empty="${2:-0}" val
    while :; do
        printf '%s' "$prompt" >&2
        if ! IFS= read -r val; then
            echo "" >&2
            return 1
        fi
        if [ -n "$val" ] || [ "$allow_empty" = "1" ]; then
            printf '%s' "$val"
            return 0
        fi
        echo "  输入不能为空, 请重新输入" >&2
    done
}

# 读取密码 (不回显), 结果打印到 stdout
read_secret() {
    local prompt="$1" val
    printf '%s' "$prompt" >&2
    if ! IFS= read -rs val; then
        echo "" >&2
        return 1
    fi
    echo "" >&2
    if [ -z "$val" ]; then
        echo "  输入不能为空, 请重新输入" >&2
        return 1
    fi
    printf '%s' "$val"
}

# 显式 y/n 确认: 0=是 1=否 (不提供回车默认)
ask_yes_no() {
    local prompt="$1" ans
    while :; do
        printf '%s [y/n]: ' "$prompt"
        if ! IFS= read -r ans; then
            echo ""
            return 1
        fi
        case "$ans" in
            y|Y|yes|YES|Yes) return 0 ;;
            n|N|no|NO|No)    return 1 ;;
            *) echo "  请输入 y 或 n" ;;
        esac
    done
}

# 暂停等待返回
pause() {
    printf '\n按回车返回上一级菜单... '
    IFS= read -r _ || true
    echo ""
}

# 选择节点: 结果写入 PICKED_NODE (支持编号或节点名/简写)
pick_node() {
    PICKED_NODE=""
    local prompt="$1"
    local -a names=()
    local n i=1 ans r

    while IFS= read -r n; do
        [ -n "$n" ] && names+=("$n")
    done < <(node_names 2>/dev/null)

    if [ "${#names[@]}" -eq 0 ]; then
        log_err "节点清单中没有可用节点 (检查 ${INVENTORY_DIR}/nodes.yaml)"
        return 1
    fi

    echo ""
    echo "  编号 节点名             IP               主机名         角色"
    echo "  ----------------------------------------------------------------------------"
    for n in "${names[@]}"; do
        printf '  %-4s %-18s %-16s %-14s %s\n' \
            "$i" "$n" "$(node_ip "$n")" "$(node_hostname "$n")" "$(node_role "$n")"
        i=$((i + 1))
    done
    echo ""

    while :; do
        printf '%s (编号或节点名): ' "$prompt"
        if ! IFS= read -r ans; then
            echo ""
            return 1
        fi
        if [ -z "$ans" ]; then
            echo "  输入不能为空, 请重新输入"
            continue
        fi
        case "$ans" in
            *[!0-9]*)
                if r="$(node_resolve "$ans" 2>/dev/null)"; then
                    PICKED_NODE="$r"
                    return 0
                fi
                echo "  未找到节点: $ans"
                ;;
            *)
                if [ "$ans" -ge 1 ] && [ "$ans" -le "${#names[@]}" ]; then
                    PICKED_NODE="${names[$((ans - 1))]}"
                    return 0
                fi
                echo "  编号超出范围 (1-${#names[@]})"
                ;;
        esac
    done
}

# 查找可用的 python 解释器
# 实现集中在 scripts/lib-pydeps.sh, 此处只做委派 (库缺失时退化为内联实现)
pick_python() {
    if declare -F pydeps_pick_python >/dev/null 2>&1; then
        pydeps_pick_python
        return $?
    fi
    local c
    for c in python3 python; do
        if command -v "$c" >/dev/null 2>&1; then
            echo "$c"
            return 0
        fi
    done
    return 1
}

# 执行封装: 先展示命令, 再要求显式确认
run_script() {
    local desc="$1"; shift
    log_step "$desc"
    echo -e "  ${DIM}命令: $*${NC}"
    echo ""
    if ! ask_yes_no "确认执行?"; then
        log_warn "已取消"
        return 1
    fi
    echo ""
    "$@"
    local rc=$?
    echo ""
    if [ "$rc" -eq 0 ]; then
        log_ok "$desc —— 完成"
    else
        log_err "$desc —— 失败 (退出码 $rc)"
    fi
    return "$rc"
}

# 列出节点清单
show_nodes() {
    echo ""
    echo "  节点名               IP                 主机名            角色"
    echo "  ------------------------------------------------------------------------"
    local n
    for n in $(node_names); do
        printf '  %-20s %-18s %-17s %s\n' \
            "$n" "$(node_ip "$n")" "$(node_hostname "$n")" "$(node_role "$n")"
    done
    echo ""
}

# ------------------------------------------------------------
# 5. 部署 Panel 面板
# ------------------------------------------------------------
panel_install_deps() {
    local req="${PANEL_DIR}/requirements.txt"
    if [ ! -f "$req" ]; then
        log_warn "未找到 panel/requirements.txt, 跳过依赖安装"
        return 0
    fi
    local py
    py="$(pick_python)" || {
        log_err "未检测到 python3, 请先安装: apt-get install -y python3 python3-pip"
        return 1
    }

    # 解析 requirements, 得出「模块名」(import 校验) 与「发行版包名」(apt 兜底)
    local spec p
    spec="$(pydeps_read_requirements "$req" | tr '\n' ' ')"
    if [ -z "$spec" ]; then
        log_warn "panel/requirements.txt 无有效条目, 跳过"
        return 0
    fi
    local -a mods=() apts=()
    for p in $spec; do
        mods+=("$(pydeps_module_name "$p")")
        apts+=("$(pydeps_apt_name "$p")")
    done

    echo -e "  ${DIM}解释器: ${py} -> $(command -v "$py" 2>/dev/null)${NC}"

    # 依赖已齐就不用动系统
    if pydeps_verify "$py" "${mods[@]}"; then
        log_ok "Python 依赖已就绪 (${spec})"
        return 0
    fi
    echo ""

    # ---- 先解决「pip 本身缺失」 ----
    # Debian 12+ / Armbian 上 python3 存在但 pip 模块没装的场景,
    # 此时 --break-system-packages 完全无效 (它只是 pip 的旗标, 不能补 pip)
    if ! pydeps_pip_usable "$py"; then
        log_warn "${py} 缺少 pip 模块 (常见于: 装了 python3 但未装 python3-pip)"
        log_warn "  注意: --break-system-packages 只是 pip 的旗标, 补不了 pip 自身"
        if pydeps_try_ensurepip "$py"; then
            log_ok "已通过 ensurepip 补齐 pip"
        elif command -v apt-get >/dev/null 2>&1; then
            echo ""
            echo -e "  ${DIM}命令: ${SUDO:+$SUDO }apt-get install -y python3-pip${NC}"
            if ask_yes_no "是否用 apt 补齐 pip (python3-pip)?"; then
                if pydeps_try_apt_pip "$SUDO" && pydeps_pip_usable "$py"; then
                    log_ok "已通过 apt 补齐 pip"
                else
                    log_warn "apt 安装 python3-pip 未成功"
                fi
            else
                log_warn "已跳过补齐 pip"
            fi
        fi
    fi

    # ---- 路线 1/2: pip ----
    if pydeps_pip_usable "$py"; then
        echo ""
        echo -e "  ${DIM}命令: ${SUDO:+$SUDO }${py} -m pip install -r panel/requirements.txt${NC}"
        if ( cd "$PANEL_DIR" && $SUDO "$py" -m pip install -r requirements.txt ); then
            if pydeps_verify "$py" "${mods[@]}"; then
                log_ok "Python 依赖安装完成"
                return 0
            fi
            log_warn "pip 报告成功, 但 import 校验未通过"
        fi
        log_warn "常规安装失败, 尝试 --break-system-packages (Debian 12+ / PEP 668)"
        if ( cd "$PANEL_DIR" && $SUDO "$py" -m pip install --break-system-packages -r requirements.txt ); then
            if pydeps_verify "$py" "${mods[@]}"; then
                log_ok "Python 依赖安装完成 (--break-system-packages)"
                return 0
            fi
            log_warn "pip 报告成功, 但 import 校验未通过"
        fi
    fi

    # ---- 路线 4: 发行版包, 完全绕开 pip ----
    if command -v apt-get >/dev/null 2>&1; then
        echo ""
        log_warn "pip 路线不可用或未成功, 可改用系统包 (完全绕开 pip)"
        echo -e "  ${DIM}命令: ${SUDO:+$SUDO }apt-get install -y ${apts[*]}${NC}"
        if ask_yes_no "是否改用系统包安装 (${apts[*]})?"; then
            if pydeps_try_apt_pkgs "$SUDO" "${apts[@]}"; then
                if pydeps_verify "$py" "${mods[@]}"; then
                    log_ok "Python 依赖已由系统包安装"
                    return 0
                fi
                log_warn "系统包装完但 import 校验仍未通过"
            fi
        fi
    fi

    log_err "Python 依赖安装失败 (缺少模块: ${mods[*]})"
    log_info "可手工执行以下任一条:"
    pydeps_hint "$spec" "$py" "$SUDO"
    return 1
}

# 由监听地址推导可访问地址 (0.0.0.0 / 127.0.0.1 都不能直接当访问地址用)
panel_access_hint() {
    local host="$1" port="$2" a out=""
    case "$host" in
        0.0.0.0)
            while IFS= read -r a; do
                out="${out}${out:+, }http://${a}:${port}"
            done < <(panel_detect_local_ipv4)
            if [ -n "$out" ]; then
                printf '%s\n' "$out"
            else
                printf 'http://<本机IP>:%s\n' "$port"
            fi
            ;;
        127.0.0.1)
            printf 'http://127.0.0.1:%s (仅本机; 远端可 ssh -L %s:127.0.0.1:%s <用户>@<节点IP>)\n' \
                "$port" "$port" "$port"
            ;;
        *)
            printf 'http://%s:%s\n' "$host" "$port"
            ;;
    esac
}

# 手动输入监听地址: 非法值给原因与替代值, 非本机地址要求显式确认
panel_choose_host_manual() {
    local input
    while :; do
        input="$(read_input '请输入监听地址 (IPv4, 如 10.20.30.40): ')" || return 1

        if ! panel_host_check "$input"; then
            log_err "$PANEL_HOST_REASON"
            if [ -n "$PANEL_HOST_SUGGEST" ]; then
                log_info "建议改为: ${PANEL_HOST_SUGGEST}"
                if ask_yes_no "是否改用 ${PANEL_HOST_SUGGEST}?"; then
                    PANEL_HOST="$PANEL_HOST_SUGGEST"
                    PANEL_HOST_DESC="$(panel_host_desc "$PANEL_HOST")"
                    return 0
                fi
            fi
            continue
        fi

        # 合法但不在本机网卡上: 绑定时会失败, 必须让用户明确知道
        if [ "$input" != "0.0.0.0" ] && ! panel_host_is_local "$input"; then
            log_warn "${input} 不在本机任何网卡上, 面板启动会失败 (Cannot assign requested address)"
            ask_yes_no "仍然使用这个地址?" || continue
        fi

        PANEL_HOST="$input"
        PANEL_HOST_DESC="$(panel_host_desc "$PANEL_HOST")"
        return 0
    done
}

# 选择监听地址: 先给三个常见取向, 需要别的地址再走手动输入
#   要点: 想「同网段可访问」应绑定本机在该网段的地址 ——
#   绑 0.0.0.0 会在所有网卡 (含 WireGuard / 外网) 上一起监听, 暴露面更大
panel_choose_host() {
    local lan_ip
    lan_ip="$(panel_detect_local_ipv4 | head -n 1)"

    while :; do
        # 地址列按字节固定宽度对齐 (地址均为 ASCII; 中文描述不参与填充)
        menu "请选择面板监听地址" \
            "$(printf '%-15s' '0.0.0.0')   全部网卡 (含 WireGuard / 外网网卡, 暴露面最大)" \
            "$(printf '%-15s' "${lan_ip:--}")   本机局域网地址 (同网段可访问, 推荐)" \
            "$(printf '%-15s' '127.0.0.1')   仅本机 (远端访问需 SSH 端口转发)" \
            "手动输入其它 IPv4 地址"

        case "$MENU_CHOICE" in
            1)
                PANEL_HOST="0.0.0.0"
                PANEL_HOST_DESC="$(panel_host_desc "$PANEL_HOST")"
                log_warn "0.0.0.0 会在全部网卡上监听, 请确保已用防火墙限制来源并设置了面板认证"
                return 0
                ;;
            2)
                if [ -n "$lan_ip" ]; then
                    PANEL_HOST="$lan_ip"
                    PANEL_HOST_DESC="$(panel_host_desc "$PANEL_HOST")"
                    log_ok "监听地址 ${PANEL_HOST} —— 同网段可直接访问, 其它网段需经路由/防火墙"
                    return 0
                fi
                log_warn "未能自动探测到本机 IPv4 地址, 请手动输入"
                if panel_choose_host_manual; then
                    return 0
                fi
                ;;
            3)
                PANEL_HOST="127.0.0.1"
                PANEL_HOST_DESC="$(panel_host_desc "$PANEL_HOST")"
                log_warn "仅监听 127.0.0.1: 本机浏览器可访问; 远端访问需 SSH 端口转发"
                return 0
                ;;
            *)
                if panel_choose_host_manual; then
                    return 0
                fi
                ;;
        esac
    done
}

panel_collect() {
    PANEL_PORT="$(read_input '请输入面板监听端口 (例 9000): ')" || return 1
    case "$PANEL_PORT" in
        ''|*[!0-9]*)
            log_err "端口必须是数字"
            return 1
            ;;
    esac
    if [ "$PANEL_PORT" -lt 1 ] || [ "$PANEL_PORT" -gt 65535 ]; then
        log_err "端口范围应为 1-65535"
        return 1
    fi

    panel_choose_host || return 1
    PANEL_USER="$(read_input '请输入面板登录用户名: ')" || return 1
    PANEL_PASS="$(read_secret '请输入面板登录密码 (输入时不回显): ')" || return 1

    # 端口占用提示
    if command -v ss >/dev/null 2>&1; then
        if ss -tln 2>/dev/null | grep -qE "[:.]${PANEL_PORT}[[:space:]]"; then
            log_warn "端口 ${PANEL_PORT} 当前已被监听占用"
            ask_yes_no "仍然继续?" || return 1
        fi
    fi
    return 0
}

panel_install_systemd() {
    if ! command -v systemctl >/dev/null 2>&1; then
        log_err "当前系统没有 systemd, 请改用「仅前台试运行」"
        return 1
    fi

    log_info "写入环境文件 /etc/onecloud/panel.env"
    $SUDO mkdir -p /etc/onecloud || return 1
    printf 'PANEL_HOST=%s\nPANEL_PORT=%s\nPANEL_USER=%s\nPANEL_PASS=%s\n' \
        "$PANEL_HOST" "$PANEL_PORT" "$PANEL_USER" "$PANEL_PASS" \
        | $SUDO tee /etc/onecloud/panel.env >/dev/null || return 1
    $SUDO chmod 600 /etc/onecloud/panel.env

    log_info "写入 systemd 覆盖片段 (注入上述环境变量)"
    $SUDO mkdir -p /etc/systemd/system/onecloud-panel.service.d || return 1
    printf '# 由 init/init.sh 生成: 注入面板监听地址与认证配置\n[Service]\nEnvironmentFile=-/etc/onecloud/panel.env\n' \
        | $SUDO tee /etc/systemd/system/onecloud-panel.service.d/10-init-override.conf >/dev/null || return 1

    log_info "安装 systemd 服务 (复用 panel/install-service.sh)"
    # 同时以环境变量注入监听参数, 与上面的 EnvironmentFile 形成双保险
    # (用 env 而非 VAR= 前缀, 避免 sudo 的 env_reset 把变量丢掉)
    # ONECLOUD_PANEL_TTY=0: 监听地址/端口已在本菜单问过, 别让安装脚本再问一遍
    #        (访问地址由脚本按本机地址自动填充)
    #        刻意不用命令行开关表达 —— 那等于预置默认值, 与本脚本的约定冲突
    $SUDO env PANEL_HOST="$PANEL_HOST" PANEL_PORT="$PANEL_PORT" \
        ONECLOUD_PANEL_TTY=0 bash "${PANEL_DIR}/install-service.sh" || return 1

    $SUDO systemctl daemon-reload
    $SUDO systemctl restart onecloud-panel
    sleep 1
    if $SUDO systemctl is-active --quiet onecloud-panel; then
        log_ok "onecloud-panel.service 运行中"
        return 0
    fi
    log_err "onecloud-panel.service 未处于运行状态, 请查看: systemctl status onecloud-panel"
    return 1
}

panel_deploy() {
    local mode="$1"
    log_step "部署 Panel 面板 (运行方式: $mode)"

    if [ ! -f "${PANEL_DIR}/app.py" ]; then
        log_err "未找到 ${PANEL_DIR}/app.py"
        pause
        return 1
    fi

    local py
    if ! py="$(pick_python)"; then
        log_err "未检测到 python3, 请先安装: apt-get install -y python3 python3-pip"
        pause
        return 1
    fi

    # 可选: 由清单重新生成面板配置
    if [ -f "${SCRIPTS_DIR}/gen-panel-config.sh" ]; then
        if ask_yes_no "是否先由 inventory/nodes.yaml 重新生成 panel/config.json?"; then
            ( cd "$PROJECT_ROOT" && bash scripts/gen-panel-config.sh ) || log_warn "生成失败, 继续使用现有配置"
        fi
    fi

    # 可选: 安装依赖
    if ask_yes_no "是否安装/更新 Python 依赖 (panel/requirements.txt)?"; then
        panel_install_deps || { pause; return 1; }
    fi

    # 逐项收集配置 (全部必填, 无默认值)
    panel_collect || { pause; return 1; }

    echo ""
    echo -e "  ${BOLD}配置汇总${NC}"
    echo "    监听地址 : ${PANEL_HOST}"
    [ -n "${PANEL_HOST_DESC:-}" ] && echo "               ${PANEL_HOST_DESC}"
    echo "    监听端口 : ${PANEL_PORT}"
    echo "    登录用户 : ${PANEL_USER}"
    echo "    登录密码 : (已设置, ${#PANEL_PASS} 位)"
    echo "    Python   : ${py}"
    echo "    运行方式 : ${mode}"
    echo ""
    if ! ask_yes_no "确认按以上配置部署面板?"; then
        log_warn "已取消"
        pause
        return 1
    fi

    if [ "$mode" = "foreground" ]; then
        log_info "前台启动面板, 按 Ctrl+C 结束"
        PANEL_CONFIG="${PANEL_DIR}/config.json" \
        PANEL_HOST="$PANEL_HOST" PANEL_PORT="$PANEL_PORT" \
        PANEL_USER="$PANEL_USER" PANEL_PASS="$PANEL_PASS" \
            "$py" "${PANEL_DIR}/app.py"
        log_info "面板前台进程已结束"
    else
        panel_install_systemd || { pause; return 1; }
        echo ""
        log_ok "面板部署完成"
        log_info "访问地址: $(panel_access_hint "$PANEL_HOST" "$PANEL_PORT")  (用户: ${PANEL_USER})"
        log_info "环境文件: /etc/onecloud/panel.env (权限 600)"
    fi
    pause
}

menu_panel() {
    while :; do
        header "部署 Panel 控制面板"
        echo -e "  ${DIM}面板目录: ${PANEL_DIR}${NC}"
        log_info "面板部署作用于「运行本入口的这台机器」；如需装到其它节点, 请先在该节点运行本入口"
        menu "请选择操作" \
            "部署为 systemd 常驻服务 (开机自启)" \
            "仅前台试运行 (Ctrl+C 结束, 不写 systemd)" \
            "仅安装 Python 依赖" \
            "返回主菜单"
        case "$MENU_CHOICE" in
            1) panel_deploy systemd ;;
            2) panel_deploy foreground ;;
            3)
                panel_install_deps
                pause
                ;;
            4) return ;;
        esac
    done
}

# ------------------------------------------------------------
# 6. 部署节点
# ------------------------------------------------------------
node_bootstrap_local() {
    log_step "本机节点初始化 (bootstrap.sh)"
    local bs="${SCRIPTS_DIR}/bootstrap.sh"
    if [ ! -f "$bs" ]; then
        log_err "未找到 ${bs}"
        pause
        return 1
    fi

    echo -e "  将执行: ${BOLD}${SUDO:+$SUDO }bash scripts/bootstrap.sh${NC}"
    echo -e "  ${DIM}不附加任何参数; 主机名 / IP / 网关 / SD 卡等全部由 bootstrap 交互提问${NC}"
    echo ""
    if ! ask_yes_no "确认开始本机初始化?"; then
        log_warn "已取消"
        pause
        return 1
    fi

    ( cd "$PROJECT_ROOT" && $SUDO bash scripts/bootstrap.sh )
    local rc=$?
    echo ""
    if [ "$rc" -eq 0 ]; then
        log_ok "bootstrap 执行结束"
    else
        log_err "bootstrap 退出码: $rc"
    fi
    pause
}

node_open_remote() {
    log_step "打开远程节点的交互入口 (SSH 接力)"
    echo -e "  ${DIM}在目标节点上运行同一个入口, 由你在远端菜单里继续选择要执行的功能${NC}"

    pick_node "请选择目标节点" || { pause; return 1; }
    local node="$PICKED_NODE"
    local ip
    ip="$(node_ip "$node")"

    local ssh_user remote_path
    ssh_user="$(read_input '请输入 SSH 登录用户 (例 root): ')" || { pause; return 1; }
    remote_path="$(read_input '请输入目标节点上的项目绝对路径 (例 /root/onecloud-cluster-workbuddy): ')" || { pause; return 1; }

    echo ""
    log_info "测试 SSH 连接 ${ssh_user}@${ip} ..."
    if ssh -o ConnectTimeout=5 -o BatchMode=yes "${ssh_user}@${ip}" "echo ok" >/dev/null 2>&1; then
        log_ok "免密登录可用"
    else
        log_warn "免密登录不可用, 继续尝试交互式登录 (会提示输入密码)"
    fi

    if ! ssh -o ConnectTimeout=5 "${ssh_user}@${ip}" "test -f '${remote_path}/init/init.sh'" >/dev/null 2>&1; then
        log_err "目标节点上未找到 ${remote_path}/init/init.sh"
        echo "  请先在目标节点获取本项目, 例如:"
        echo "      git clone <仓库地址> ${remote_path}"
        pause
        return 1
    fi

    echo ""
    if ! ask_yes_no "确认在 ${node} (${ip}) 上打开交互入口?"; then
        log_warn "已取消"
        pause
        return 1
    fi

    ssh -t -o ConnectTimeout=10 "${ssh_user}@${ip}" "cd '${remote_path}' && bash init/init.sh"
    local rc=$?
    echo ""
    [ "$rc" -eq 0 ] && log_ok "远端交互入口已退出" || log_warn "远端会话退出码: $rc"
    pause
}

menu_deploy_node() {
    while :; do
        header "部署节点"
        echo -e "  ${DIM}bootstrap 用于把一台刚刷好 Armbian 的玩客云初始化成集群节点${NC}"
        menu "请选择方式" \
            "本机初始化 (当前机器就是待部署节点, 需 root)" \
            "打开远程节点的交互入口 (SSH 接力)" \
            "查看节点清单" \
            "返回主菜单"
        case "$MENU_CHOICE" in
            1) node_bootstrap_local ;;
            2) node_open_remote ;;
            3)
                show_nodes
                pause
                ;;
            4) return ;;
        esac
    done
}

# ------------------------------------------------------------
# 7. 节点维护
# ------------------------------------------------------------
maint_health() {
    if run_script "集群健康巡检" bash "${SCRIPTS_DIR}/health-check.sh"; then
        :
    fi
    pause
}

maint_backup() {
    header "备份配置与数据"
    menu "请选择备份范围" \
        "全量备份 (所有节点)" \
        "仅配置文件" \
        "仅应用数据" \
        "指定单个节点" \
        "指定单个服务" \
        "返回"
    local sel="$MENU_CHOICE"
    if [ "$sel" = "6" ]; then
        return
    fi

    local bdir
    bdir="$(read_input '请输入备份根目录 (例 /mnt/sd/backups): ')" || { pause; return 1; }

    local -a args=()
    case "$sel" in
        1) args=(all) ;;
        2) args=(config) ;;
        3) args=(data) ;;
        4)
            pick_node "请选择要备份的节点" || { pause; return 1; }
            args=(node "$PICKED_NODE")
            ;;
        5)
            local svc
            svc="$(read_input '请输入服务名 (例 homeassistant): ')" || { pause; return 1; }
            args=(service "$svc")
            ;;
    esac

    log_step "备份"
    echo "  备份目录: ${bdir}"
    echo "  参数    : ${args[*]}"
    echo ""
    if ask_yes_no "确认执行备份?"; then
        ( cd "$PROJECT_ROOT" && export BACKUP_DIR="$bdir" && bash scripts/backup.sh "${args[@]}" )
        local rc=$?
        echo ""
        [ "$rc" -eq 0 ] && log_ok "备份完成" || log_err "备份失败 (退出码 $rc)"
    else
        log_warn "已取消"
    fi
    pause
}

maint_restore() {
    header "从备份恢复"

    local bdir
    bdir="$(read_input '请输入备份根目录 (例 /mnt/sd/backups): ')" || { pause; return 1; }

    echo ""
    echo -e "  ${BOLD}可用备份 (最近 10 个)${NC}"
    if [ -d "$bdir" ]; then
        ls -1t "$bdir" 2>/dev/null | head -10 | sed 's/^/    /'
    else
        log_warn "目录不存在: ${bdir}"
    fi
    echo ""

    local bid
    bid="$(read_input "请输入备份 ID (或输入 latest 使用最新一次): ")" || { pause; return 1; }

    menu "请选择恢复范围" \
        "全部 (all)" \
        "仅配置 (config)" \
        "指定节点" \
        "指定服务" \
        "返回"
    local sel="$MENU_CHOICE"
    if [ "$sel" = "5" ]; then
        return
    fi

    local -a args=("$bid")
    case "$sel" in
        1) args+=(all) ;;
        2) args+=(config) ;;
        3)
            pick_node "请选择要恢复的节点" || { pause; return 1; }
            args+=(node "$PICKED_NODE")
            ;;
        4)
            local svc
            svc="$(read_input '请输入服务名 (例 homeassistant): ')" || { pause; return 1; }
            args+=(service "$svc")
            ;;
    esac

    log_step "恢复"
    echo -e "  ${RED}${BOLD}恢复会覆盖目标上的现有配置/数据, 请确认备份 ID 与范围无误${NC}"
    echo "  备份目录: ${bdir}"
    echo "  参数    : ${args[*]}"
    echo ""
    if ask_yes_no "确认执行恢复?"; then
        ( cd "$PROJECT_ROOT" && export BACKUP_DIR="$bdir" && bash scripts/restore.sh "${args[@]}" )
        local rc=$?
        echo ""
        [ "$rc" -eq 0 ] && log_ok "恢复完成" || log_err "恢复失败 (退出码 $rc)"
    else
        log_warn "已取消"
    fi
    pause
}

maint_update() {
    header "批量更新"
    menu "请选择更新内容" \
        "仅更新 Docker 镜像" \
        "仅更新系统包" \
        "全部更新 (镜像 + 系统包)" \
        "返回"
    local sel="$MENU_CHOICE"
    if [ "$sel" = "4" ]; then
        return
    fi

    local -a args=()
    case "$sel" in
        1) args=(-d) ;;
        2) args=(-s) ;;
        3) args=(-a) ;;
    esac

    if ask_yes_no "是否限定到单个节点?"; then
        pick_node "请选择节点" || { pause; return 1; }
        args+=(-n "$PICKED_NODE")
    fi

    run_script "批量更新" bash "${SCRIPTS_DIR}/update-all.sh" "${args[@]}"
    pause
}

# 生成防火墙设置建议清单
#
# 部署脚本不改防火墙, 所以"该开哪些端口"这件事需要一个显式出口:
# 本函数只做静态生成 (读 nodes.yaml + services.yaml), 不碰运行时规则。
maint_fw_recommend() {
    header "生成防火墙设置建议清单"
    echo -e "  ${DIM}onecloud 的部署脚本不改任何节点的防火墙。${NC}"
    echo -e "  ${DIM}本操作按节点清单与服务声明, 静态算出「该放行哪些端口」,${NC}"
    echo -e "  ${DIM}写入 docs/firewall/<节点>.txt; 真正改规则的是你手动执行的 setup_firewall.sh。${NC}"
    echo ""
    if run_script "生成防火墙建议清单" \
        bash "${SCRIPTS_DIR}/firewall-recommend.sh"; then
        log_info "下一步: 把清单拷到对应节点, 执行 setup_firewall.sh 逐条录入并应用"
    fi
    pause
}

menu_maintenance() {
    while :; do
        header "节点维护"
        menu "请选择操作" \
            "集群健康巡检 (SSH / 容器 / 端口 / 负载 / OOM / WireGuard)" \
            "备份配置与数据" \
            "从备份恢复" \
            "批量更新镜像 / 系统包" \
            "生成防火墙设置建议清单 (不改防火墙, 供 setup_firewall.sh 使用)" \
            "返回主菜单"
        case "$MENU_CHOICE" in
            1) maint_health ;;
            2) maint_backup ;;
            3) maint_restore ;;
            4) maint_update ;;
            5) maint_fw_recommend ;;
            6) return ;;
        esac
    done
}

# ------------------------------------------------------------
# 8. 配置与分发
# ------------------------------------------------------------
cfg_deploy() {
    header "分发配置到节点"
    menu "请选择分发范围" \
        "分发到所有节点" \
        "仅分发到指定节点" \
        "返回"
    case "$MENU_CHOICE" in
        1)
            run_script "分发配置到所有节点" bash "${SCRIPTS_DIR}/deploy.sh"
            pause
            ;;
        2)
            pick_node "请选择节点" || { pause; return 1; }
            run_script "分发配置到 ${PICKED_NODE}" bash "${SCRIPTS_DIR}/deploy.sh" -n "$PICKED_NODE"
            pause
            ;;
        3) return ;;
    esac
}

cfg_exec() {
    header "分发后在节点上远程执行命令"
    log_warn "该命令会以 root 身份在目标节点上执行, 请谨慎输入"
    local cmd
    cmd="$(read_input '请输入要远程执行的命令 (例 docker-compose up -d): ')" || { pause; return 1; }

    echo ""
    echo "  将先分发配置, 再在节点执行: ${BOLD}${cmd}${NC}"
    if ! ask_yes_no "确认执行?"; then
        log_warn "已取消"
        pause
        return 1
    fi
    ( cd "$PROJECT_ROOT" && bash scripts/deploy.sh --exec "$cmd" )
    local rc=$?
    echo ""
    [ "$rc" -eq 0 ] && log_ok "远程执行完成" || log_err "远程执行失败 (退出码 $rc)"
    pause
}

cfg_gen_panel() {
    run_script "生成面板配置 panel/config.json" bash "${SCRIPTS_DIR}/gen-panel-config.sh" || true
    pause
}

cfg_gen_env() {
    header "渲染节点 .env 文件"
    local node=""
    if ask_yes_no "是否只渲染单个节点?"; then
        pick_node "请选择节点" || { pause; return 1; }
        node="$PICKED_NODE"
    fi

    echo ""
    if ask_yes_no "是否先预览 (--dry-run) 而不写入?"; then
        if [ -n "$node" ]; then
            run_script "预览 ${node} 的 .env" bash "${SCRIPTS_DIR}/gen-node-env.sh" "$node" --dry-run
        else
            run_script "预览全部节点的 .env" bash "${SCRIPTS_DIR}/gen-node-env.sh" --dry-run
        fi
    else
        if [ -n "$node" ]; then
            run_script "渲染 ${node} 的 .env" bash "${SCRIPTS_DIR}/gen-node-env.sh" "$node"
        else
            run_script "渲染全部节点的 .env" bash "${SCRIPTS_DIR}/gen-node-env.sh"
        fi
    fi
    pause
}

wg_add_peer() {
    header "登记 WireGuard 节点"
    local name ip wgip
    name="$(read_input '请输入节点名 (例 wk-backup-04): ')" || { pause; return 1; }
    ip="$(read_input '请输入节点 LAN IP (例 192.168.1.104): ')" || { pause; return 1; }
    wgip="$(read_input '请输入节点 WireGuard IP (例 10.8.0.104): ')" || { pause; return 1; }

    run_script "登记 WireGuard 节点 ${name}" \
        bash "${SCRIPTS_DIR}/wireguard-setup.sh" add peer "$name" "$ip" "$wgip"
    pause
}

menu_wireguard() {
    while :; do
        header "WireGuard 配置管理"
        menu "请选择操作" \
            "生成全部节点密钥与 wg0.conf" \
            "登记新节点 (add peer)" \
            "列出已登记节点" \
            "返回"
        case "$MENU_CHOICE" in
            1)
                run_script "生成 WireGuard 配置" bash "${SCRIPTS_DIR}/wireguard-setup.sh" gen
                pause
                ;;
            2) wg_add_peer ;;
            3)
                run_script "列出 WireGuard 节点" bash "${SCRIPTS_DIR}/wireguard-setup.sh" list
                pause
                ;;
            4) return ;;
        esac
    done
}

menu_config() {
    while :; do
        header "配置与分发"
        menu "请选择操作" \
            "分发配置到节点 (deploy.sh)" \
            "仅测试节点 SSH 连接" \
            "预览将要分发的文件" \
            "分发后在节点上远程执行命令" \
            "生成面板配置 panel/config.json" \
            "渲染节点 .env 文件" \
            "WireGuard 配置管理" \
            "返回主菜单"
        case "$MENU_CHOICE" in
            1) cfg_deploy ;;
            2)
                run_script "测试节点 SSH 连接" bash "${SCRIPTS_DIR}/deploy.sh" -t
                pause
                ;;
            3)
                run_script "预览分发内容" bash "${SCRIPTS_DIR}/deploy.sh" -d
                pause
                ;;
            4) cfg_exec ;;
            5) cfg_gen_panel ;;
            6) cfg_gen_env ;;
            7) menu_wireguard ;;
            8) return ;;
        esac
    done
}

# ------------------------------------------------------------
# 9. 服务安装
# ------------------------------------------------------------
svc_native() {
    header "安装原生服务"
    menu "请选择服务" \
        "mihomo (Clash Meta 代理)" \
        "xiaomusic (小爱音乐)" \
        "migpt (AI 助手)" \
        "verysync (微力同步)" \
        "全部原生服务 (all-native)" \
        "返回"
    local sel="$MENU_CHOICE"
    if [ "$sel" = "6" ]; then
        return
    fi

    local target=""
    case "$sel" in
        1) target="mihomo" ;;
        2) target="xiaomusic" ;;
        3) target="migpt" ;;
        4) target="verysync" ;;
        5) target="all-native" ;;
    esac

    run_script "安装原生服务: ${target}" \
        $SUDO bash "${SCRIPTS_DIR}/install-services.sh" "$target"
    pause
}

svc_docker() {
    header "启动节点容器"
    menu "请选择节点角色" \
        "edge (NODE-01 全部容器)" \
        "iot (NODE-02 全部容器)" \
        "storage (NODE-03 全部容器)" \
        "全部容器 (all-docker)" \
        "返回"
    local sel="$MENU_CHOICE"
    if [ "$sel" = "5" ]; then
        return
    fi

    local target=""
    case "$sel" in
        1) target="edge" ;;
        2) target="iot" ;;
        3) target="storage" ;;
        4) target="all-docker" ;;
    esac

    run_script "启动容器: ${target}" \
        $SUDO bash "${SCRIPTS_DIR}/install-services.sh" "$target"
    pause
}

menu_services() {
    while :; do
        header "服务安装"
        menu "请选择操作" \
            "统一安装 (交互式多选 + 端口冲突检测, setup.sh)" \
            "安装原生服务 (mihomo / xiaomusic / migpt / verysync)" \
            "启动节点容器 (edge / iot / storage)" \
            "返回主菜单"
        case "$MENU_CHOICE" in
            1)
                run_script "统一安装" $SUDO bash "${SCRIPTS_DIR}/setup.sh"
                pause
                ;;
            2) svc_native ;;
            3) svc_docker ;;
            4) return ;;
        esac
    done
}

# ------------------------------------------------------------
# 10. 环境自检
# ------------------------------------------------------------
menu_selfcheck() {
    header "环境自检"
    local problems=0

    echo -e "${BOLD}项目结构${NC}"
    local d
    for d in scripts panel inventory docs init; do
        if [ -d "${PROJECT_ROOT}/${d}" ]; then
            log_ok "目录存在: ${d}/"
        else
            log_err "缺少目录: ${d}/"
            problems=$((problems + 1))
        fi
    done

    echo ""
    echo -e "${BOLD}关键脚本${NC}"
    local s
    for s in lib-nodes.sh lib-pydeps.sh lib-panel-host.sh lib-network-audit.sh \
             bootstrap.sh deploy.sh \
             health-check.sh backup.sh \
             restore.sh update-all.sh install-services.sh setup.sh \
             wireguard-setup.sh gen-panel-config.sh gen-node-env.sh \
             firewall-recommend.sh; do
        if [ -f "${SCRIPTS_DIR}/${s}" ]; then
            log_ok "存在: scripts/${s}"
        else
            log_err "缺失: scripts/${s}"
            problems=$((problems + 1))
        fi
    done

    # ---- Python 环境 (面板/ migpt 的依赖前提) ----
    echo ""
    echo -e "${BOLD}Python 环境${NC}"
    local py
    if py="$(pick_python)"; then
        log_ok "解释器: ${py} -> $(command -v "$py" 2>/dev/null)"
        if pydeps_pip_usable "$py"; then
            log_ok "pip 模块可用 ($("$py" -m pip --version 2>/dev/null | head -1))"
        else
            log_warn "${py} 缺少 pip 模块 —— 面板依赖安装会先失败一次"
            log_info "  修复: ${SUDO:+$SUDO }apt-get install -y python3-pip"
            log_info "  或安装时改走系统包: ${SUDO:+$SUDO }apt-get install -y python3-flask python3-flask-cors"
        fi
        local m missing=""
        for m in flask flask_cors; do
            if ! pydeps_verify "$py" "$m"; then
                missing="${missing:+$missing }$m"
            fi
        done
        if [ -z "$missing" ]; then
            log_ok "面板依赖已就绪 (flask, flask_cors)"
        else
            log_warn "面板缺少模块: ${missing} (部署面板时选择安装依赖即可)"
        fi
    else
        log_warn "未检测到 python3 —— 面板与 migpt 无法运行"
        log_info "  安装: ${SUDO:+$SUDO }apt-get install -y python3 python3-pip"
    fi

    echo ""
    echo -e "${BOLD}依赖命令${NC}"
    local c
    for c in bash ssh rsync git python3; do
        if command -v "$c" >/dev/null 2>&1; then
            log_ok "$c: $(command -v "$c")"
        else
            log_warn "未找到 ${c} (相关功能可能不可用)"
        fi
    done
    if command -v docker >/dev/null 2>&1; then
        log_ok "docker: $(command -v docker)"
    else
        log_warn "未找到 docker (容器相关功能需在节点上执行)"
    fi

    echo ""
    echo -e "${BOLD}本机网络${NC}"
    local lip
    lip="$(hostname -I 2>/dev/null | awk '{print $1}')" || lip=""
    [ -n "$lip" ] && log_info "本机 IP: ${lip}" || log_warn "未能读取本机 IP"

    echo ""
    echo -e "${BOLD}节点清单${NC}"
    if [ "$HAVE_INVENTORY" = "1" ] && [ "${#NODE_NAMES[@]}" -gt 0 ]; then
        show_nodes
    else
        log_warn "未加载到任何节点 (检查 ${INVENTORY_DIR}/nodes.yaml)"
        problems=$((problems + 1))
    fi

    if [ "$problems" -eq 0 ]; then
        log_ok "自检完成, 未发现结构性问题"
    else
        log_warn "自检完成, 发现 ${problems} 项问题"
    fi
    pause
}

# ------------------------------------------------------------
# 11. 主菜单
# ------------------------------------------------------------
preflight_check() {
    if [ ! -d "$SCRIPTS_DIR" ]; then
        log_err "未找到 scripts/ 目录, 当前路径可能不是项目根: ${PROJECT_ROOT}"
        exit 1
    fi
    if [ "$HAVE_INVENTORY" != "1" ]; then
        log_warn "节点清单库未加载, 依赖清单的功能将不可用"
    fi
}

print_banner() {
    clear 2>/dev/null || true
    echo ""
    echo -e "${MAGENTA}${BOLD}╔══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${MAGENTA}${BOLD}║        OneCloud 集群 · 交互式初始化入口                  ║${NC}"
    echo -e "${MAGENTA}${BOLD}╚══════════════════════════════════════════════════════════╝${NC}"
    echo -e "  ${DIM}项目目录: ${PROJECT_ROOT}${NC}"
    echo -e "  ${DIM}纯交互模式: 不接受命令行参数, 所有选项与取值均需手动输入${NC}"
}

main_menu() {
    echo ""
    menu "请选择功能" \
        "部署 Panel 控制面板" \
        "部署节点 (新节点初始化)" \
        "节点维护 (巡检 / 备份 / 恢复 / 更新)" \
        "配置与分发 (deploy / WireGuard / 配置生成)" \
        "服务安装 (统一安装 / 原生服务 / 节点容器)" \
        "环境自检" \
        "退出"
}

main() {
    print_banner
    preflight_check

    while :; do
        main_menu
        case "$MENU_CHOICE" in
            1) menu_panel ;;
            2) menu_deploy_node ;;
            3) menu_maintenance ;;
            4) menu_config ;;
            5) menu_services ;;
            6) menu_selfcheck ;;
            7)
                echo ""
                log_info "已退出"
                exit 0
                ;;
        esac
    done
}

main
