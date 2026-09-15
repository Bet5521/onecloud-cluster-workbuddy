#!/bin/bash
# ============================================================
# Python 依赖安装公共库 (lib-pydeps.sh)
# 由 init/init.sh 与 scripts/install-services.sh 共同 source
# ============================================================
#
# 背景 —— Debian 12+ / Armbian (玩客云) 上的典型故障:
#   `python3` 存在, 但 pip 模块并未安装, 于是
#       python3 -m pip install -r req.txt  ->  No module named pip
#       pip3 install ...                   ->  command not found
#   此时再加 `--break-system-packages` 毫无用处 —— 它只是 pip 的旗标,
#   用于绕过 PEP 668 (externally-managed-environment), 不能补 pip 本身。
#
# 本库按「由轻到重」四路降级, 任一路成功且 import 验证通过即返回 0:
#   1) 已有 pip        -> pip install                          (常规)
#   2) 同上 + PEP 668  -> pip install --break-system-packages
#   3) pip 缺失        -> ensurepip -> apt python3-pip -> get-pip.py, 再回 1/2
#   4) pip 彻底不可用  -> apt 安装发行版包 (python3-flask 等), 完全绕开 pip
#
# 关键设计: 面板 systemd 单元执行 /usr/bin/python3, 因此依赖必须装进
#   **系统解释器**, 不能用 venv —— 否则服务起来照样 ModuleNotFoundError。
#
# 约定:
#   - 只定义函数, source 它不产生任何副作用
#   - 需要 root 的命令通过调用方传入的 SUDO 变量执行 (空串 = 已是 root)
#   - 不调用 exit, 一律 return 状态码
#   - 不定义 log_* (各调用方命名不同, 避免互相覆盖)
#   - 进度信息走 stderr, 便于调用方把 stdout 留给数据
# ============================================================

PYDEPS_SUDO="${PYDEPS_SUDO:-}"

pydeps_say() { echo "[pydeps] $*" >&2; }

pydeps_have_cmd() { command -v "${1:-}" >/dev/null 2>&1; }

# 查找可用的 python 解释器 (优先 python3)
pydeps_pick_python() {
    local c
    for c in python3 python; do
        if pydeps_have_cmd "$c"; then
            echo "$c"
            return 0
        fi
    done
    return 1
}

# pip 是否真的可用 —— python3 存在但 pip 模块缺失时这里会失败
pydeps_pip_usable() {
    local py="${1:-}"
    if [ -z "$py" ]; then return 1; fi
    if "$py" -m pip --version >/dev/null 2>&1; then return 0; fi
    return 1
}

# 包名 -> 发行版包名 (与 install-services.sh 既有约定保持一致)
pydeps_apt_name() {
    case "${1:-}" in
        flask)         echo python3-flask ;;
        flask-cors)    echo python3-flask-cors ;;
        pyyaml|yaml)   echo python3-yaml ;;
        requests)      echo python3-requests ;;
        *)             echo "python3-${1:-}" ;;
    esac
}

# 包名 -> import 用的模块名
pydeps_module_name() {
    case "${1:-}" in
        flask-cors) echo flask_cors ;;
        pyyaml)     echo yaml ;;
        *)          printf '%s' "${1:-}" | tr '-' '_' ;;
    esac
}

# 归一化成裸包名: "flask>=2.0  # 注释" -> "flask"
pydeps_strip_spec() {
    printf '%s' "${1:-}" \
        | sed -e 's/#.*//' -e 's/[[:space:]]//g' -e 's/[<>=!~].*$//'
}

# 读 requirements.txt, 每行一个裸包名 (跳过空行/注释)
pydeps_read_requirements() {
    local f="${1:-}" line
    if [ ! -f "$f" ]; then return 1; fi
    while IFS= read -r line || [ -n "$line" ]; do
        line="$(pydeps_strip_spec "$line")"
        if [ -n "$line" ]; then printf '%s\n' "$line"; fi
    done < "$f"
    return 0
}

# 断言模块可 import —— 不轻信安装命令的退出码
pydeps_verify() {
    local py="${1:-}"; shift
    if [ -z "$py" ]; then return 1; fi
    local mods="" m
    for m in "$@"; do
        mods="${mods:+$mods, }$m"
    done
    if [ -z "$mods" ]; then return 0; fi
    if "$py" -c "import $mods" >/dev/null 2>&1; then return 0; fi
    return 1
}

# --- 补齐 pip 的三条子路线 -------------------------------------

pydeps_try_ensurepip() {
    local py="${1:-}"
    if pydeps_pip_usable "$py"; then return 0; fi
    pydeps_say "尝试 ensurepip 补齐 pip ..."
    "$py" -m ensurepip --upgrade >/dev/null 2>&1 \
        || "$py" -m ensurepip --default-pip >/dev/null 2>&1 \
        || true
    if pydeps_pip_usable "$py"; then return 0; fi
    pydeps_say "ensurepip 不可用 (Debian 通常需先装 python3-venv)"
    return 1
}

pydeps_try_apt_pip() {
    local sudo="${1:-$PYDEPS_SUDO}"
    if ! pydeps_have_cmd apt-get; then return 1; fi
    pydeps_say "apt-get install -y python3-pip ..."
    $sudo apt-get update -qq >/dev/null 2>&1 || true
    $sudo apt-get install -y python3-pip \
        || { pydeps_say "apt 安装 python3-pip 失败"; return 1; }
    return 0
}

pydeps_try_getpip() {
    local py="${1:-}" sudo="${2:-$PYDEPS_SUDO}" getter=""
    if pydeps_have_cmd curl; then
        getter="curl -fsSL --max-time 30"
    elif pydeps_have_cmd wget; then
        getter="wget -qO- --timeout=30"
    else
        return 1
    fi
    pydeps_say "尝试 get-pip.py 兜底 (需要外网) ..."
    # 直接管道喂给解释器 (python3 - 从 stdin 读程序), 不落临时文件:
    # 既不依赖 rm, 也少一个失败点。下载为空时解释器读到 EOF 即刻结束,
    # 由下面的 pip 可用性检查兜住, 不会误判成功。
    if $getter https://bootstrap.pypa.io/get-pip.py 2>/dev/null \
       | $sudo "$py" - >/dev/null 2>&1; then
        if pydeps_pip_usable "$py"; then return 0; fi
    fi
    pydeps_say "get-pip.py 路线失败 (无外网 / DNS 不通 / 下载内容异常)"
    return 1
}

# 补齐 pip 的完整链条 (供非交互调用方使用)
pydeps_ensure_pip() {
    local py="${1:-}" sudo="${2:-$PYDEPS_SUDO}"
    if pydeps_pip_usable "$py"; then return 0; fi
    if pydeps_try_ensurepip "$py"; then return 0; fi
    if pydeps_try_apt_pip "$sudo" && pydeps_pip_usable "$py"; then return 0; fi
    if pydeps_try_getpip "$py" "$sudo"; then return 0; fi
    return 1
}

# --- 发行版包路线 ----------------------------------------------

pydeps_try_apt_pkgs() {
    local sudo="${1:-$PYDEPS_SUDO}"; shift
    if ! pydeps_have_cmd apt-get; then return 1; fi
    if [ "$#" -eq 0 ]; then return 1; fi
    pydeps_say "改用系统包 (绕开 pip): apt-get install -y $*"
    $sudo apt-get update -qq >/dev/null 2>&1 || true
    $sudo apt-get install -y "$@" \
        || { pydeps_say "系统包安装失败: $*"; return 1; }
    return 0
}

# --- 主入口 ----------------------------------------------------

# pydeps_install <py> "<包名 空格分隔>" [sudo]
#   spec 可带版本约束, 如 "flask>=2.0 flask-cors"
#   返回 0 表示安装完成 **且** 模块已可 import
pydeps_install() {
    local py="${1:-}" spec="${2:-}" sudo="${3:-$PYDEPS_SUDO}"
    if [ -z "$py" ] || [ -z "$spec" ]; then return 1; fi

    local pkgs=() apts=() mods=() p
    for p in $spec; do
        p="$(pydeps_strip_spec "$p")"
        if [ -z "$p" ]; then continue; fi
        pkgs+=("$p")
        apts+=("$(pydeps_apt_name "$p")")
        mods+=("$(pydeps_module_name "$p")")
    done
    if [ "${#pkgs[@]}" -eq 0 ]; then return 1; fi

    # 若依赖已齐, 直接返回, 不打扰包管理器
    if pydeps_verify "$py" "${mods[@]}"; then
        pydeps_say "依赖已就绪, 跳过安装"
        return 0
    fi

    # 路线 1/2: pip (必要时先补 pip)
    if pydeps_pip_usable "$py" || pydeps_ensure_pip "$py" "$sudo"; then
        pydeps_say "pip install ${pkgs[*]}"
        if ! $sudo "$py" -m pip install "${pkgs[@]}"; then
            pydeps_say "常规 pip 安装失败, 重试 --break-system-packages (Debian 12+ / PEP 668)"
            $sudo "$py" -m pip install --break-system-packages "${pkgs[@]}" || true
        fi
        if pydeps_verify "$py" "${mods[@]}"; then return 0; fi
        pydeps_say "pip 路线未能满足依赖, 继续降级到系统包"
    else
        pydeps_say "pip 不可用 (python3-pip 未安装且无法自动补齐)"
    fi

    # 路线 4: 发行版包, 完全绕开 pip
    if pydeps_try_apt_pkgs "$sudo" "${apts[@]}"; then
        if pydeps_verify "$py" "${mods[@]}"; then return 0; fi
    fi
    return 1
}

# pydeps_install_from_file <py> <requirements.txt> [sudo]
pydeps_install_from_file() {
    local py="${1:-}" f="${2:-}" sudo="${3:-$PYDEPS_SUDO}"
    if [ ! -f "$f" ]; then
        pydeps_say "未找到依赖清单: $f"
        return 1
    fi
    local spec
    spec="$(pydeps_read_requirements "$f" | tr '\n' ' ')"
    if [ -z "$spec" ]; then
        pydeps_say "依赖清单为空: $f"
        return 0
    fi
    pydeps_install "$py" "$spec" "$sudo"
}

# 手工兜底命令 (失败时的提示文案用)
# pydeps_hint "<包名 空格分隔>" [py] [sudo]
pydeps_hint() {
    local spec="${1:-}" py="${2:-python3}" sudo="${3:-$PYDEPS_SUDO}"
    local apts=() p
    for p in $spec; do
        apts+=("$(pydeps_apt_name "$(pydeps_strip_spec "$p")")")
    done
    echo "    ${sudo:+$sudo }$py -m pip install --break-system-packages $spec"
    echo "    ${sudo:+$sudo }apt-get install -y ${apts[*]:-}"
    echo "    (如 python3-pip 本身缺失, 先执行: ${sudo:+$sudo }apt-get install -y python3-pip)"
}
