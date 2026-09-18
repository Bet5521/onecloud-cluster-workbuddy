#!/bin/bash
# ============================================================
# OneCloud Cluster - 面板配置生成器
# 依据 inventory/nodes.yaml (节点身份 + 服务列表) 与
# inventory/services.yaml (container 标志) 生成 panel/config.json。
#
# 这样面板的节点 IP / 主机名 永远与清单一致:
#   - 修改 nodes.yaml 中的 ip / wg_ip / hostname 后,
#     重新运行本脚本即可刷新面板, 无需手改 config.json。
#   - 支持 nodes.local.yaml / 环境变量覆盖 (见 nodes.yaml 头部说明)。
# 用法: bash scripts/gen-panel-config.sh [--out FILE_OR_DIR]
#   --out  指定输出 (默认仓库内 panel/config.json; 传目录则写 <目录>/config.json)
#          面板已安装到稳定目录时, 用 --out /opt/onecloud/panel 同步到实际运行目录
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
source "$SCRIPT_DIR/lib-nodes.sh"
# 安装态 / 组网模式 / 数据根 单一真相 (见 docs/design-optional-components.md §2)
# shellcheck source=lib-services.sh
source "$SCRIPT_DIR/lib-services.sh"

VERSION="${ONECLOUD_PANEL_VERSION:-1.6.0}"

# 输出路径: 命令行 > 环境变量 > 仓库内 panel/config.json
#   --out FILE  写指定文件; 若传入的是已存在的目录, 则写 <目录>/config.json
PANEL_CONFIG="${ONECLOUD_PANEL_CONFIG:-${ROOT_DIR}/panel/config.json}"
while [ $# -gt 0 ]; do
    case "$1" in
        -o|--out) PANEL_CONFIG="${2:-}"; shift 2 ;;
        -h|--help)
            sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
            echo "用法: bash scripts/gen-panel-config.sh [--out FILE_OR_DIR]"
            exit 0 ;;
        *) echo "[ERROR] 未知选项: $1" >&2; exit 2 ;;
    esac
done
[ -n "$PANEL_CONFIG" ] || { echo "[ERROR] 输出路径为空" >&2; exit 2; }
# 传入目录时补全文件名
if [ -d "$PANEL_CONFIG" ]; then
    PANEL_CONFIG="${PANEL_CONFIG%/}/config.json"
fi

# 通用日志
log_info()  { echo -e "\033[0;32m[INFO]\033[0m $*"; }
log_error() { echo -e "\033[0;31m[ERROR]\033[0m $*"; }

# 服务名 -> 显示名 (仅展示用)
declare -A DISPLAY=(
    [cloudflared]="Cloudflare Tunnel" [adguard]="AdGuard Home"
    [wireguard]="WireGuard"           [clash]="Clash"
    [memos]="Memos"                   [homeassistant]="Home Assistant"
    [xiaomusic]="xiaomusic"           [migpt]="migpt"
    [piwigo]="Piwigo"                 [typecho]="Typecho"
    [syncthing]="Syncthing"           [verysync]="verysync"
    [aria2]="aria2"                   [ariang]="AriaNg"
    [cupsd]="CUPS"                    [cups-web]="CUPS Web"
    [gitea]="Gitea"                   [panel]="OneCloud Panel"
)

# 服务名 -> container 标志 (来自 services.yaml)
declare -A CONTAINER
while IFS='|' read -r svc cont; do
    [ -z "${svc:-}" ] && continue
    case "$cont" in
        false|"false") CONTAINER["$svc"]=false ;;
        *)             CONTAINER["$svc"]=true  ;;
    esac
done < <(awk '
    BEGIN { cur="" }
    {
        line=$0; sub(/\r$/,"",line)
        if (line ~ /^[[:space:]]*#/ || line ~ /^[[:space:]]*$/) next
        if (line ~ /^[A-Za-z_]/) { cur=""; next }
        if (line ~ /^  [A-Za-z_][A-Za-z0-9_-]*:/) {
            sub(/^  /,"",line); sub(/:.*$/,"",line)
            gsub(/^[[:space:]]+|[[:space:]]+$/,"",line); cur=line; next
        }
        if (cur!="" && line ~ /^    container:/) {
            sub(/^    container:/,"",line); gsub(/^[[:space:]]+|[[:space:]]+$/,"",line)
            print cur "|" line; cur=""
        }
    }' "${ROOT_DIR}/inventory/services.yaml")

require_nodes

# 逐节点生成 services 子 JSON
# 新增字段 (见 docs/design-optional-components.md §4.2):
#   installed 是否"应当安装" (清单声明 + 模式判定 + 安装方式)
#   optional  是否可选组件
#   install   空 / manual / external
#   port      主端口 (0 = 无)
build_services() {
    local n="$1" s d c inst opt imode port out=""
    for s in $(node_services "$n"); do
        d="${DISPLAY[$s]:-$s}"
        c="${CONTAINER[$s]:-true}"
        inst="$(service_installed "$n" "$s")"
        opt="$(services_optional "$s")"
        imode="$(services_install_mode "$s")"
        port="$(services_port "$s")"
        [ "$inst" = "1" ] && inst="true" || inst="false"
        [ "$opt"  = "1" ] && opt="true"  || opt="false"
        [ -n "$out" ] && out="${out},"
        out="${out}{\"name\":\"$s\",\"display\":\"$d\",\"container\":$c,\"installed\":$inst,\"optional\":$opt,\"install\":\"$imode\",\"port\":$port}"
    done
    echo "$out"
}

# 输出目录不存在时先建 (--out 指定了稳定目录的场景)
mkdir -p "$(dirname "$PANEL_CONFIG")" 2>/dev/null || true

# 逐行拼装, 避免多行变量拼接导致的逗号丢失
{
    echo "{"
    echo '  "cluster_name": "OneCloud Cluster",'
    echo "  \"version\": \"${VERSION}\","
    echo "  \"network_mode\": \"$(network_mode)\","
    echo "  \"network_mode_label\": \"$(network_mode_label)\","
    echo "  \"wg_enabled\": $([ "$(wg_enabled)" = "1" ] && echo true || echo false),"
    echo "  \"wg_subnet\": \"${NET_WG_SUBNET}\","
    echo "  \"lan_subnet\": \"${NET_LAN_SUBNET}\","
    echo '  "update_interval": 10,'
    echo '  "ssh_timeout": 3,'
    echo '  "generated_by": "scripts/gen-panel-config.sh",'
    echo '  "nodes": ['

    first=1
    for n in $(node_names); do
        [ "$first" -eq 0 ] && echo "  ,"
        first=0

        name="$n"
        display="$(node_display_name "$n")"; [ -z "$display" ] && display="$(node_role "$n")"
        role="$(node_role "$n")"
        ip="$(node_ip "$n")"
        wg="$(node_wg_ip "$n")"
        host="$(node_hostname "$n")"
        color="$(node_color "$n")"; [ -z "$color" ] && color="#9E9E9E"
        svc="$(build_services "$n")"
        # 该节点数据根 (面板执行 docker 命令与查磁盘都用它, 不再拼 /mnt/sd)
        # 用 oc_static_data_root: 生成器只写默认值, 面板运行时会自己探测真实根;
        # 这里若走 oc_data_root 会对每个节点 SSH, 离线时整脚本白等数秒。
        droot="$(oc_static_data_root)"

        echo "  {"
        echo "    \"name\": \"$name\","
        echo "    \"display_name\": \"$display\","
        echo "    \"role\": \"$role\","
        echo "    \"ip\": \"$ip\","
        echo "    \"wg_ip\": \"$wg\","
        echo "    \"hostname\": \"$host\","
        echo "    \"color\": \"$color\","
        echo "    \"data_root\": \"$droot\","
        echo "    \"services\": [${svc}]"
        echo "  }"
    done

    echo "  ]"
    echo "}"
} > "$PANEL_CONFIG"

echo "[OK] 已生成面板配置: $PANEL_CONFIG ($(node_names | wc -l) 个节点)"