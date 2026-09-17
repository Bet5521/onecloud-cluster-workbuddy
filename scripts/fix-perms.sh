#!/bin/bash
# ============================================================
# OneCloud Cluster - 脚本执行权限批量修复 (fix-perms.sh)
#
# 背景
#   git 对可执行位的记录依赖 filemode。在 Windows 上克隆 (core.fileMode=false)、
#   或通过「非 git 方式」拷贝/FAT 文件系统搬运后, 仓库里 scripts/*.sh、
#   init/init.sh 及 node-*/**.sh 的 +x 位会丢失, 直接 `./scripts/xxx.sh` 会报
#   "Permission denied"。本脚本把仓库内所有 shell 脚本恢复为可执行。
#
# 覆盖范围
#   <仓库根>/**/*.sh              (含 scripts/ init/ panel/ node-*/ 等)
#   排除 .git/ 目录
#   可选 --with-py: 顺带给 panel/app.py 等 .py 入口加 +x (python 脚本本不必,
#                   便于 `./app.py` 直接运行)
#
# 用法
#   bash scripts/fix-perms.sh            # 修复 (幂等, 无变更时不动)
#   bash scripts/fix-perms.sh --list     # 只列出当前不可执行的 .sh
#   bash scripts/fix-perms.sh --dry-run  # 只显示将要 chmod 的文件, 不改动
#   bash scripts/fix-perms.sh --root DIR # 指定仓库根 (默认脚本上级目录)
#   bash scripts/fix-perms.sh --with-py  # 同时修复 .py
#
# 退出码: 0=成功 (含"无需修改"); 1=参数错误或路径不存在
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

log_info()  { echo -e "\033[0;32m[INFO]\033[0m $*"; }
log_warn()  { echo -e "\033[1;33m[WARN]\033[0m $*"; }
log_error() { echo -e "\033[0;31m[ERROR]\033[0m $*"; }
log_ok()    { echo -e "\033[0;32m[ OK ]\033[0m $*"; }

MODE="fix"          # fix | list | dry-run
WITH_PY=0

while [ $# -gt 0 ]; do
    case "$1" in
        -l|--list)     MODE="list"; shift ;;
        -n|--dry-run)  MODE="dry-run"; shift ;;
        --with-py)     WITH_PY=1; shift ;;
        --root)        ROOT_DIR="${2:-}"; shift 2 ;;
        -h|--help)
            sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) log_error "未知选项: $1"; exit 1 ;;
    esac
done

[ -d "$ROOT_DIR" ] || { log_error "仓库根不存在: ${ROOT_DIR}"; exit 1; }

# 收集目标文件 (排除 .git)
collect() {
    find "$ROOT_DIR" -type d -name .git -prune -o \
        -type f -name '*.sh' -print 2>/dev/null | sort
    if [ "$WITH_PY" = 1 ]; then
        find "$ROOT_DIR" -type d -name .git -prune -o \
            -type f -name '*.py' -print 2>/dev/null | sort
    fi
}

# 是否已可执行
is_exec() { [ -x "$1" ]; }

fixed=0
skipped=0
total=0

while IFS= read -r f; do
    [ -n "$f" ] || continue
    total=$((total + 1))
    if is_exec "$f"; then
        skipped=$((skipped + 1))
        continue
    fi
    case "$MODE" in
        list)
            echo "  [不可执行] ${f#"$ROOT_DIR"/}"
            ;;
        dry-run)
            echo "  [待修复]   chmod +x ${f#"$ROOT_DIR"/}"
            ;;
        fix)
            if chmod +x "$f" 2>/dev/null; then
                echo "  [已修复]   ${f#"$ROOT_DIR"/}"
                fixed=$((fixed + 1))
            else
                log_warn "chmod 失败: $f"
            fi
            ;;
    esac
done < <(collect)

echo ""
case "$MODE" in
    list)
        log_info "共 ${total} 个脚本, 其中不可执行 $((total - skipped)) 个"
        ;;
    dry-run)
        log_info "共 ${total} 个脚本, 其中待修复 $((total - skipped)) 个 (--dry-run, 未改动)"
        ;;
    fix)
        if [ "$fixed" -eq 0 ]; then
            log_ok "无需修改: ${total} 个脚本均已具备可执行位"
        else
            log_ok "已修复 ${fixed} 个脚本 (共 ${total} 个)"
        fi
        ;;
esac

# 提示 git 侧根治: 把可执行位写入索引, 避免下次克隆再次丢失
if [ "$MODE" = "fix" ] && [ -d "${ROOT_DIR}/.git" ]; then
    echo ""
    log_info "提示: 若要让「克隆/拉取」默认带上可执行位, 请执行一次:"
    echo "        git update-index --chmod=+x \$(git ls-files '*.sh')"
    echo "        git commit -m 'chore: 记录脚本可执行位'"
fi
exit 0
