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
# 用法: bash scripts/gen-panel-config.sh
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
source "$SCRIPT_DIR/lib-nodes.sh"

PANEL_CONFIG="${ROOT_DIR}/panel/config.json"
VERSION="${ONECLOUD_PANEL_VERSION:-1.2.0}"

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
build_services() {
    local n="$1" s d c out=""
    for s in $(node_services "$n"); do
        d="${DISPLAY[$s]:-$s}"
        c="${CONTAINER[$s]:-true}"
        [ -n "$out" ] && out="${out},"
        out="${out}{\"name\":\"$s\",\"display\":\"$d\",\"container\":$c}"
    done
    echo "$out"
}

# 逐行拼装, 避免多行变量拼接导致的逗号丢失
{
    echo "{"
    echo '  "cluster_name": "OneCloud Cluster",'
    echo "  \"version\": \"${VERSION}\","
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

        echo "  {"
        echo "    \"name\": \"$name\","
        echo "    \"display_name\": \"$display\","
        echo "    \"role\": \"$role\","
        echo "    \"ip\": \"$ip\","
        echo "    \"wg_ip\": \"$wg\","
        echo "    \"hostname\": \"$host\","
        echo "    \"color\": \"$color\","
        echo "    \"services\": [${svc}]"
        echo "  }"
    done

    echo "  ]"
    echo "}"
} > "$PANEL_CONFIG"

echo "[OK] 已生成面板配置: $PANEL_CONFIG ($(node_names | wc -l) 个节点)"
