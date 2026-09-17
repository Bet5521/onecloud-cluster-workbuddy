#!/bin/bash
# ============================================================
# OneCloud Cluster - 安装路径自适应库 (lib-install-path.sh)
#
# 解决的问题
#   玩客云是无图形界面 / 1GB 内存 / eMMC 的"无头服务器", 多数节点会插一张
#   SD 卡来扩容, 但「无卡 / 未挂载 / 挂载只读 / 空间不足」都是正常状态, 不应
#   阻断初始化或安装流程。本库在**初始化(首次启动)与安装阶段**统一决策:
#
#     探测 SD 卡设备 -> 是否已被挂载 -> 挂载点可读写 -> 剩余空间是否足够
#        ↓ 全部满足                       ↓ 任意一项不满足
#     安装到 SD 卡挂载点                   回退安装到 /opt/onecloud
#
# 设计原则
#   * 挂载路径一律「运行时查询」(findmnt/mountpoint), 不写死 /mnt/sd 常量。
#   * SD 卡永远不是硬性前置: 任何失败都安全降级到 /opt, 不中断流程。
#   * 写入 SD 卡过程中若失败, 自动回退 /opt 并记录日志、输出明确状态。
#   * 与外层 log_* 共存: 若外层已定义 log_info/log_warn/log_error 则复用,
#     否则提供内置实现, 保证本库可单独 source / 直接测试。
#
# 用法
#   source lib-install-path.sh
#   resolve_data_root                 # 决策 DATA_ROOT (SD 卡 or /opt)
#   install_path_for srv              # -> $DATA_ROOT/srv
#   safe_install_dir  "srv/foo" "foo" # 建目录, SD 失败自动降级
#   safe_install_file "srv/foo/x" "$body" "foo 配置"  # 写文件, 同上
#
# 可覆盖的环境变量
#   INSTALL_FALLBACK_ROOT  回退根目录 (默认 /opt/onecloud)
#   SD_MIN_SPACE_MB       SD 卡判定为可用所需的最小可用空间(MB, 默认 512)
#   OC_TEST_NO_SYSBLOCK   测试钩子: =1 时跳过 /sys/block 兜底探测
#   ONECLOUD_SD_TEST_FORCE_DEGRADE 测试钩子: =1 时 safe_install_* 强制走降级分支
# ============================================================

# ---- 日志: 复用外层或提供内置 ----
if ! declare -F log_info >/dev/null 2>&1; then
    log_info()  { echo "[INFO] $*"; }
fi
if ! declare -F log_warn >/dev/null 2>&1; then
    log_warn()  { echo "[WARN] $*"; }
fi
if ! declare -F log_error >/dev/null 2>&1; then
    log_error() { echo "[ERROR] $*"; }
fi

# ---- 可覆盖常量 ----
INSTALL_FALLBACK_ROOT="${INSTALL_FALLBACK_ROOT:-/opt/onecloud}"
SD_MIN_SPACE_MB="${SD_MIN_SPACE_MB:-512}"

# 这些全局变量是决策结果的载体, 供调用方读取:
#   SD_FOUND        0/1 是否探测到 SD 设备
#   SD_DEV          设备名(不含 /dev/, 如 mmcblk1)
#   SD_PART         分区设备路径(如 /dev/mmcblk1p1)
#   SD_MOUNTED      0/1 分区是否已挂载
#   SD_MOUNT_POINT  实际挂载点(运行时查询得到)
#   SD_USABLE       0/1 综合评估是否可作为安装目标
#   SD_REJECT_REASON 不可用时的人类可读原因
#   DATA_ROOT       最终选定的安装根目录
#   INSTALL_VIA_SD  0/1 是否走 SD 卡
#   INSTALL_SOURCE  决策来源描述(用于日志/状态展示)

# ----------------------------------------------------------------------------
# 1. 探测 SD 卡设备与分区
#    判据(任一满足即视为候选): lsblk 可移动标记 / USB 通道 / 非根 mmcblk 设备
#    写全局: SD_FOUND SD_DEV SD_PART
# ----------------------------------------------------------------------------
sd_probe() {
    SD_FOUND=0; SD_DEV=""; SD_PART=""
    local _root_src _root_dev _name _rm _type _tran _d
    # 测试钩子: 直接注入设备名(如 mmcblk1), 跳过真实块设备探测, 保证用例确定性
    if [ -n "${ONECLOUD_SD_TEST_DEV:-}" ]; then
        SD_DEV="${ONECLOUD_SD_TEST_DEV}"
        SD_PART="${ONECLOUD_SD_TEST_PART:-/dev/${SD_DEV}p1}"
        SD_FOUND=1
        return 0
    fi
    _root_src="$(findmnt -n -o SOURCE / 2>/dev/null | awk '{print $1}')" || true
    _root_dev="$(basename "${_root_src:-}" 2>/dev/null || true)"
    # 剥离分区后缀: mmcblk0p1 -> mmcblk0 ; sda1 -> sda
    case "$_root_dev" in
        mmcblk*p[0-9]*)     _root_dev="${_root_dev%p[0-9]*}" ;;
        [shv]d[a-z][0-9]*)  _root_dev="${_root_dev%[0-9]}" ;;
    esac

    # 第一轮: 高置信度 (lsblk 的 RM / TRAN)
    if command -v lsblk >/dev/null 2>&1; then
        while read -r _name _rm _type _tran; do
            [ -n "$_name" ] || continue
            [ "$_type" = "disk" ] || continue
            case "$_name" in
                mmcblk*|sd[a-z]) ;;
                *) continue ;;
            esac
            [ "$_name" = "$_root_dev" ] && continue
            if [ "$_rm" = "1" ] || [ "$_tran" = "usb" ]; then
                SD_DEV="$_name"; break
            fi
        done < <(lsblk -dno NAME,RM,TYPE,TRAN 2>/dev/null || true)
    fi

    # 第二轮: /sys/block 兜底 (无 lsblk 的精简系统)
    if [ "${OC_TEST_NO_SYSBLOCK:-0}" != "1" ] && [ -z "$SD_DEV" ] && [ -d /sys/block ]; then
        for _d in /sys/block/*; do
            [ -d "$_d" ] || continue
            _name="$(basename "$_d")"
            case "$_name" in
                mmcblk*|sd[a-z]) ;;
                *) continue ;;
            esac
            [ "$_name" = "$_root_dev" ] && continue
            if [ "$(cat "$_d/removable" 2>/dev/null || echo 0)" = "1" ]; then
                SD_DEV="$_name"; break
            fi
        done
    fi

    # 第三轮: 低置信度启发式 —— 非根分区以外的 mmcblk 视为 SD
    # (玩客云 eMMC 常为 mmcblk0, 插卡为 mmcblk1; 仅在无更可靠线索时启用)
    if [ "${OC_TEST_NO_SYSBLOCK:-0}" != "1" ] && [ -z "$SD_DEV" ] && [ -d /sys/block ]; then
        for _d in /sys/block/*; do
            [ -d "$_d" ] || continue
            _name="$(basename "$_d")"
            case "$_name" in
                mmcblk*) ;;
                *) continue ;;
            esac
            [ "$_name" = "$_root_dev" ] && continue
            SD_DEV="$_name"; break
        done
    fi

    if [ -z "$SD_DEV" ]; then
        SD_FOUND=0
        return 1
    fi

    # 定位分区: 优先 <dev>p1, 退而求其次 <dev>1, 都没有则为空(整卡直挂由调用方决定)
    if [ -b "/dev/${SD_DEV}p1" ]; then
        SD_PART="/dev/${SD_DEV}p1"
    elif [ -b "/dev/${SD_DEV}1" ]; then
        SD_PART="/dev/${SD_DEV}1"
    else
        SD_PART=""
    fi
    SD_FOUND=1
    return 0
}

# ----------------------------------------------------------------------------
# 2. 运行时查询挂载状态 (绝不写死挂载点)
#    参数: $1 = 设备路径(如 /dev/mmcblk1p1) 或设备名
#    写全局: SD_MOUNTED SD_MOUNT_POINT
# ----------------------------------------------------------------------------
is_mountpoint() {
    local _mp="$1"
    [ -n "$_mp" ] || return 1
    if command -v mountpoint >/dev/null 2>&1; then
        mountpoint -q "$_mp" 2>/dev/null && return 0
    fi
    awk -v m="$_mp" '$2==m{exit 0} END{exit 1}' /proc/mounts 2>/dev/null
}

sd_mount_state() {
    SD_MOUNTED=0; SD_MOUNT_POINT=""
    local _dev="$1" _mp=""
    # 归一化: 已是 /dev/xxx 路径则不动, 否则补 /dev/ 前缀
    case "$_dev" in
        /dev/*) ;;
        *) _dev="/dev/$_dev" ;;
    esac

    # 优先按设备查挂载点
    if command -v findmnt >/dev/null 2>&1; then
        _mp="$(findmnt -n -o TARGET --source "$_dev" 2>/dev/null | head -1)"
    fi
    # 兜底用 mount 表
    if [ -z "$_mp" ] && command -v mount >/dev/null 2>&1; then
        _mp="$(mount 2>/dev/null | awk -v d="$_dev" '$1==d{print $3; exit}')"
    fi
    # 验证确实是挂载点
    if [ -n "$_mp" ] && is_mountpoint "$_mp"; then
        SD_MOUNT_POINT="$_mp"
        SD_MOUNTED=1
    else
        SD_MOUNT_POINT=""
        SD_MOUNTED=0
    fi
}

# ----------------------------------------------------------------------------
# 3a. 只读/可写检测: 在目标下真实创建+写入+删除一个临时文件
# ----------------------------------------------------------------------------
sd_rw_ok() {
    local _p="$1"
    [ -d "$_p" ] || mkdir -p "$_p" 2>/dev/null
    [ -d "$_p" ] || return 1
    local _t
    _t="$(mktemp -q "${_p}/.onecloud_rw.XXXXXX" 2>/dev/null)" || return 1
    if printf 'onecloud' >"$_t" 2>/dev/null && [ -s "$_t" ]; then
        rm -f "$_t" 2>/dev/null
        return 0
    fi
    rm -f "$_t" 2>/dev/null
    return 1
}

# ----------------------------------------------------------------------------
# 3b. 剩余空间检测: 可用空间(MB) >= 阈值
# ----------------------------------------------------------------------------
sd_space_ok() {
    local _p="$1" _min="$2"
    [ -d "$_p" ] || return 1
    local _avail
    _avail="$(df -Pm "$_p" 2>/dev/null | awk 'NR==2{print $4}')"
    [ -n "$_avail" ] || return 1
    [ "$_avail" -ge "${_min:-0}" ] 2>/dev/null
}

# ----------------------------------------------------------------------------
# 4. 综合评估 SD 卡是否适合作为安装目标
#    写全局: SD_USABLE SD_REJECT_REASON
# ----------------------------------------------------------------------------
sd_evaluate() {
    SD_USABLE=0; SD_REJECT_REASON=""
    if ! sd_probe; then
        SD_REJECT_REASON="未检测到 SD 卡设备"
        return 1
    fi
    if [ -z "$SD_PART" ]; then
        SD_REJECT_REASON="SD 卡无可用分区 (未分区/未格式化)"
        return 1
    fi
    sd_mount_state "$SD_PART"
    if [ "$SD_MOUNTED" != 1 ] || [ -z "$SD_MOUNT_POINT" ]; then
        SD_REJECT_REASON="SD 卡未挂载"
        return 1
    fi
    if ! sd_rw_ok "$SD_MOUNT_POINT"; then
        SD_REJECT_REASON="SD 卡挂载点只读 (${SD_MOUNT_POINT})"
        return 1
    fi
    if ! sd_space_ok "$SD_MOUNT_POINT" "${SD_MIN_SPACE_MB}"; then
        SD_REJECT_REASON="SD 卡可用空间不足 (需 ${SD_MIN_SPACE_MB}MB, 实际 $(df -Ph "$SD_MOUNT_POINT" 2>/dev/null | awk 'NR==2{print $4}') )"
        return 1
    fi
    SD_USABLE=1
    return 0
}

# ----------------------------------------------------------------------------
# 5. 核心决策: 选择数据根目录
#    优先 SD 卡(设备存在+已挂载+可读写+空间足够); 否则回退 /opt/onecloud。
#    写全局: DATA_ROOT INSTALL_VIA_SD INSTALL_SOURCE
#    始终返回 0 (绝不作为出错退出码, 避免 set -e 下意外中断)
# ----------------------------------------------------------------------------
resolve_data_root() {
    DATA_ROOT=""
    INSTALL_VIA_SD=0
    INSTALL_SOURCE=""
    if sd_evaluate; then
        DATA_ROOT="$SD_MOUNT_POINT"
        INSTALL_VIA_SD=1
        INSTALL_SOURCE="SD 卡 (${SD_DEV} -> ${SD_MOUNT_POINT})"
        log_info "安装路径: 使用 SD 卡 ${SD_MOUNT_POINT} (已挂载/可读写/空间充足)"
    else
        DATA_ROOT="$INSTALL_FALLBACK_ROOT"
        INSTALL_VIA_SD=0
        INSTALL_SOURCE="本地回退 (/opt) - ${SD_REJECT_REASON}"
        log_warn "SD 卡不可用 (${SD_REJECT_REASON}), 回退安装到 ${INSTALL_FALLBACK_ROOT}"
    fi
    return 0
}

# ----------------------------------------------------------------------------
# 6. 给定逻辑组件名, 返回其安装目录 (基于 DATA_ROOT)
#    用法: install_path_for srv|docker|backups|scripts|docs|inventory|<其它>
# ----------------------------------------------------------------------------
install_path_for() {
    local _c="$1"
    case "$_c" in
        srv)       printf '%s/srv'        "$DATA_ROOT" ;;
        docker)    printf '%s/docker'     "$DATA_ROOT" ;;
        backups)   printf '%s/backups'    "$DATA_ROOT" ;;
        scripts)   printf '%s/scripts'    "$DATA_ROOT" ;;
        docs)      printf '%s/docs'       "$DATA_ROOT" ;;
        inventory) printf '%s/inventory'  "$DATA_ROOT" ;;
        *)         printf '%s/%s' "$DATA_ROOT" "$_c" ;;
    esac
}

# ----------------------------------------------------------------------------
# 内部: 降级到 /opt 并输出明确状态
# ----------------------------------------------------------------------------
_degrade() {
    local _why="$1"
    log_error "SD 卡安装失败 (${_why}), 自动降级到 ${INSTALL_FALLBACK_ROOT}"
    DATA_ROOT="$INSTALL_FALLBACK_ROOT"
    INSTALL_VIA_SD=0
    INSTALL_SOURCE="本地回退 (/opt) - ${_why}"
}

# ----------------------------------------------------------------------------
# 7a. 安全创建目录 (支持花括号展开), SD 写入失败自动降级
#     用法: safe_install_dir <相对子路径> [标签]
# ----------------------------------------------------------------------------
safe_install_dir() {
    local _rel="$1" _label="${2:-$1}"
    local _base="${DATA_ROOT}/${_rel}"
    if mkdir -p "$_base" 2>/dev/null; then
        if [ "$INSTALL_VIA_SD" = 1 ] && [ "${ONECLOUD_SD_TEST_FORCE_DEGRADE:-0}" != "1" ] \
           && ! sd_rw_ok "$_base"; then
            _degrade "目录 ${_base} 写入校验失败"
            _base="${DATA_ROOT}/${_rel}"
            mkdir -p "$_base" 2>/dev/null || { log_error "回退后仍无法创建: ${_base}"; return 1; }
        elif [ "${ONECLOUD_SD_TEST_FORCE_DEGRADE:-0}" = "1" ] && [ "$INSTALL_VIA_SD" = 1 ]; then
            _degrade "测试钩子强制降级"
            _base="${DATA_ROOT}/${_rel}"
            mkdir -p "$_base" 2>/dev/null || { log_error "回退后仍无法创建: ${_base}"; return 1; }
        fi
        log_info "已就绪: ${_label} -> ${_base}"
        return 0
    fi
    # 走到这里说明 SD 上目录创建失败
    if [ "$INSTALL_VIA_SD" = 1 ]; then
        _degrade "创建目录 ${_base} 失败"
        _base="${DATA_ROOT}/${_rel}"
    fi
    mkdir -p "$_base" 2>/dev/null || { log_error "目录创建失败: ${_base}"; return 1; }
    log_warn "已降级: ${_label} -> ${_base} (SD 卡不可用, 已回退 /opt)"
    return 0
}

# ----------------------------------------------------------------------------
# 7b. 安全创建一组目录(花括号展开), 同上自动降级
#     用法: safe_install_tree <相对基目录> <花括号列表> [标签]
# ----------------------------------------------------------------------------
safe_install_tree() {
    local _relbase="$1" _braces="$2" _label="${3:-$_relbase}"
    local _base="${DATA_ROOT}/${_relbase}"
    if mkdir -p "${_base}"/$_braces 2>/dev/null; then
        if [ "$INSTALL_VIA_SD" = 1 ] && [ "${ONECLOUD_SD_TEST_FORCE_DEGRADE:-0}" != "1" ] \
           && ! sd_rw_ok "$_base"; then
            _degrade "目录 ${_base} 写入校验失败"
            _base="${DATA_ROOT}/${_relbase}"
            mkdir -p "${_base}"/$_braces 2>/dev/null || { log_error "回退后仍无法创建: ${_base}"; return 1; }
        elif [ "${ONECLOUD_SD_TEST_FORCE_DEGRADE:-0}" = "1" ] && [ "$INSTALL_VIA_SD" = 1 ]; then
            _degrade "测试钩子强制降级"
            _base="${DATA_ROOT}/${_relbase}"
            mkdir -p "${_base}"/$_braces 2>/dev/null || { log_error "回退后仍无法创建: ${_base}"; return 1; }
        fi
        log_info "已就绪: ${_label} -> ${_base}"
        return 0
    fi
    if [ "$INSTALL_VIA_SD" = 1 ]; then
        _degrade "创建目录 ${_base} 失败"
        _base="${DATA_ROOT}/${_relbase}"
    fi
    mkdir -p "${_base}"/$_braces 2>/dev/null || { log_error "目录创建失败: ${_base}"; return 1; }
    log_warn "已降级: ${_label} -> ${_base} (SD 卡不可用, 已回退 /opt)"
    return 0
}

# ----------------------------------------------------------------------------
# 7c. 安全写入文件, SD 失败自动降级到 /opt 同路径
#     用法: safe_install_file <相对路径/文件名> <内容> [标签]
#     内容可用 '-' 表示从 stdin 读取
# ----------------------------------------------------------------------------
safe_install_file() {
    local _rel="$1" _content="$2" _label="${3:-$1}"
    local _target="${DATA_ROOT}/${_rel}"
    mkdir -p "$(dirname "$_target")" 2>/dev/null || true
    if [ "$_content" = "-" ]; then
        if cat >"$_target" 2>/dev/null; then
            log_info "已写入: ${_label} -> ${_target}"; return 0
        fi
    else
        if printf '%s\n' "$_content" >"$_target" 2>/dev/null; then
            log_info "已写入: ${_label} -> ${_target}"; return 0
        fi
    fi
    # SD 上写入失败 -> 降级
    if [ "$INSTALL_VIA_SD" = 1 ] || [ "${ONECLOUD_SD_TEST_FORCE_DEGRADE:-0}" = "1" ]; then
        _degrade "写入 ${_target} 失败"
        _target="${DATA_ROOT}/${_rel}"
        mkdir -p "$(dirname "$_target")" 2>/dev/null || true
    fi
    if [ "$_content" = "-" ]; then
        if cat >"$_target" 2>/dev/null; then
            log_warn "已降级写入: ${_label} -> ${_target} (SD 卡不可用)"; return 0
        fi
    else
        if printf '%s\n' "$_content" >"$_target" 2>/dev/null; then
            log_warn "已降级写入: ${_label} -> ${_target} (SD 卡不可用)"; return 0
        fi
    fi
    log_error "写入失败(含回退): ${_target}"
    return 1
}
