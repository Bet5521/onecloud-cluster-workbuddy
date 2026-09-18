# 组网与组件选择性安装 —— 调整后设计方案

> 目标版本：**v1.6.0**
> 设计日期：2026-09-17
> 依据：`docs/audit-2026-09.md`（3 项 CRITICAL / 3 项 HIGH）
> 覆盖需求：
> 1. WireGuard 及其他组件支持**选择性安装**
> 2. 不装 WireGuard 时，系统**不出现任何 WireGuard 相关的组网问题**
> 3. 未安装状态在**面板上明确区分显示**
> 4. 面板能区分**每个组件**的安装与否
> 5. 适配**三种组网模式**：仅 WireGuard / 仅局域网直连 / 混合，均可正常运行与正确显示

---

## 0. 设计结论速览

| 问题 | 方案 |
| --- | --- |
| 组件是否安装，如何表达 | `inventory/services.yaml` 新增 `optional` / `default_enabled`；`nodes.yaml` 新增 `network.mode` |
| 安装态如何流向面板 | 新增 `scripts/lib-services.sh` 统一计算 `installed`，写入 `panel/config.json` |
| 面板如何显示三态 | `app.py` 先判 `installed`（未装则不探测，避免误报离线）；前端渲染「运行/停止/未安装」三态 |
| 不装 WG 时如何避免 WG 问题 | `health-check.sh` 未装即 `SKIP`；`bootstrap.sh` 不装 `wireguard-tools`；`wireguard-setup.sh` 拒绝生成；LAN 检查取代 WG 检查 |
| 三种模式如何统一 | `network.mode: auto\|wireguard\|lan\|mixed` 单一开关，派生 `WG_ENABLED` / `LAN_ONLY` 两个布尔量，所有脚本只读这两个量 |
| 数据根三套写法 | 统一 `<DATA_ROOT>/srv/<完整节点名>`，由 `lib-services.sh: service_data_dir()` 单点提供 |
| 面板不可用（C-1/C-2） | 登录页 + Cookie 会话（替代裸 Basic-Auth）；CORS origins 按实际访问地址计算 |

**核心设计原则：安装态是"清单派生的一次计算结果"，不是各脚本各自判断的。**
任何地方想知道「WireGuard 装没装」，都调用 `lib-services.sh` 的同一个函数，不允许各自读清单拼逻辑。

---

## 1. 新增的清单字段

### 1.1 `inventory/services.yaml`

每个服务新增两个字段（**向后兼容**：不写时按默认值处理，老清单不改也能跑）：

| 字段 | 取值 | 默认 | 含义 |
| --- | --- | --- | --- |
| `optional` | `true` / `false` | `false` | 是否可选组件。可选组件允许"未安装"，不算异常 |
| `default_enabled` | `true` / `false` | `true` | 首次部署时是否默认安装。仅 `optional: true` 时有意义 |
| `provides` | 字符串（可选） | 空 | 该组件提供的网络能力，用于组网模式推导（目前仅 `wireguard` 有：`wg-mesh`） |

示例（`services.yaml` 片段）：

```yaml
services:
  wireguard:
    node: wk-edge-01
    container: true
    optional: true              # 新增: 可以完全不装
    default_enabled: false      # 新增: 默认不装, 需要异地组网时显式开启
    provides: wg-mesh           # 新增: 声明它提供 WireGuard 组网能力
    image: linuxserver/wireguard:latest
    # ... 其余不变

  verysync:
    node: wk-storage-03
    container: false
    optional: true              # 新增: 需手动下载, 属可选
    default_enabled: false      # 新增: 默认不装
    install: manual             # 新增: 标记"无自动安装实现", 面板显示为「待手动安装」
    port: 19900
    # ... 其余不变

  syncthing:
    node: wk-storage-03
    container: true
    optional: false             # 默认值, 显式写出便于阅读
    default_enabled: true
    # ... 其余不变
```

**`install` 字段取值**：

| 值 | 含义 | 面板显示 |
| --- | --- | --- |
| 不写（默认） | 有自动安装实现 | 未安装时显示「未安装」+ 可安装 |
| `manual` | 无自动安装实现，需人工 | 未安装时显示「待手动安装」+ 文档链接 |
| `external` | 由外部系统管理（如 Cloudflare 侧） | 未安装时显示「未启用」 |

> **这条直接解决审计 H-1**：`verysync` 用 `install: manual` 诚实标注，面板不再把它当成"应该运行却离线"。

### 1.2 `inventory/nodes.yaml`

`network:` 段新增模式字段：

```yaml
network:
  # 组网模式 (新增):
  #   auto      自动推导 —— 有 wireguard 服务且 default_enabled -> mixed, 否则 lan
  #   wireguard 仅 WireGuard 组网 (节点间全程走隧道, 不依赖 LAN 互通)
  #   lan       仅局域网直连 (明确不装 WireGuard, 不产生任何 WG 配置与检查)
  #   mixed     混合 (LAN 直连优先, 跨网段/异地走 WireGuard) —— 默认推荐
  mode: mixed
  lan_subnet: 192.168.1.0/24
  wg_subnet: 10.8.0.0/24
  # wg_subnet / wg_port 在 mode=lan 时被忽略 (但仍可保留, 便于日后切换)
  wg_port: 51820
  gateway: 192.168.1.1
  dns: dhcp
  domain: yourdomain.com
```

**`mode` 与 `wg_subnet` / `wg_port` 的关系**：`mode=lan` 时这两个字段**不参与任何计算与校验**
（包括面板展示、bootstrap 风险预检、防火墙建议）。保留字段是为了切模式时不用重填。

---

## 2. 单一真相：`scripts/lib-services.sh`（新增）

这是本方案的核心。所有"装没装""数据在哪"的判断都从这里出。

### 2.1 模块约定

与既有库一致（`lib-nodes.sh` / `lib-pydeps.sh` / `lib-panel-host.sh` 的规矩）：

- 不 `set -e`（由调用方控制）
- 不定义 `log_*`（避免与调用方冲突）
- 只 `source lib-nodes.sh`，不重复实现清单解析
- 幂等加载守卫 `_LIB_SERVICES_LOADED`

### 2.2 对外接口

| 函数 | 签名 | 返回 | 用途 |
| --- | --- | --- | --- |
| `services_optional` | `<服务名>` | `true`/`false` | 是否可选组件 |
| `services_default_enabled` | `<服务名>` | `true`/`false` | 是否默认安装 |
| `services_install_mode` | `<服务名>` | 空/`manual`/`external` | 安装方式 |
| `services_provides` | `<服务名>` | `wg-mesh` 等 | 声明的网络能力 |
| `network_mode` | — | `wireguard`/`lan`/`mixed` | 生效的组网模式（已解析 `auto`） |
| `wg_enabled` | — | `0`/`1` | **本次部署是否启用 WireGuard** |
| `wg_enabled_on` | `<节点名>` | `0`/`1` | 该节点是否装 WireGuard |
| `node_has_service` | `<节点名> <服务名>` | `0`/`1` | 该节点的服务列表里有无此项 |
| `service_installed` | `<节点名> <服务名>` | `0`/`1` | **综合判定：清单声明 + 模式 + 手动/外部标记** |
| `service_data_dir` | `<节点名> <服务名>` | 绝对路径 | **数据目录单点**：`<DATA_ROOT>/srv/<节点名>/<服务>` |
| `node_data_dir` | `<节点名>` | 绝对路径 | `<DATA_ROOT>/srv/<节点名>` |
| `services_status_table` | — | TSV 行（固定 6 列） | 供 `install-services.sh list-installed` 消费：`节点|服务|installed|optional|install|port`。**空值以 `-` 占位**，不可输出空字段——`IFS=$'\t' read` 会折叠相邻制表符之间的空字段，导致后面的列左移错位（实测「安装方式」列显示成端口号） |

### 2.3 关键判定逻辑（伪代码，必须单点实现）

```
network_mode():
    m = NET_MODE (来自 nodes.yaml network.mode, 环境变量 ONECLOUD_NET_MODE 优先)
    if m == "auto":
        if 任一节点声明了 wireguard 且 default_enabled == true:
            return "mixed"
        return "lan"
    return m            # wireguard / lan / mixed 原样返回

wg_enabled():
    if network_mode() == "lan":
        return 0                        # 纯 LAN 模式: 强制不启用, 即使清单里有
    for n in node_names():
        if node_has_service(n, "wireguard"):
            return 1
    return 0

wg_enabled_on(n):
    if wg_enabled() == 0:
        return 0
    return node_has_service(n, "wireguard")

service_installed(n, s):
    has = node_has_service(n, s)
    [ has = 0 ]  →  return 0            # 清单没声明 = 未安装
    if s == "wireguard" and wg_enabled_on(n) == 0:
        return 0                        # WG 被模式关闭 = 未安装
    m = services_install_mode(s)
    if m == "manual" or m == "external":
        return 0                        # 无自动安装实现 = 未安装 (面板显示"待手动安装")
    return 1                            # 声明 + 可自动安装 + 未被模式关闭

service_data_dir(n, s):
    printf '%s/srv/%s/%s' "$(node_data_root_for_self_or_remote)" "$n" "$s"
```

> **`lan` 模式的强制语义**：即使 `nodes.yaml` 的 `services:` 里还留着 `wireguard`，
> `mode: lan` 也会让 `wg_enabled()` 返回 0，从而**全链路视作未安装**。
> 这样用户切模式时不必同时改两处，避免"改了 mode 忘了删服务"的中间态。

### 2.4 需要 `lib-install-path.sh` 配合的部分

`lib-services.sh` 需要知道"本机/远端"的数据根。两种场景：

| 场景 | 取值方式 |
| --- | --- |
| 本机执行（bootstrap / setup / install-services / health-check 的本地部分） | `resolve_data_root` → `$DATA_ROOT`（来自 `lib-install-path.sh`） |
| 控制端执行（deploy / backup / restore / panel） | `lib-nodes.sh: node_data_root <IP>`（SSH 读 `install.conf`） |

因此 `lib-services.sh` 提供一个统一入口：

```bash
# 数据根单点: 优先本机 DATA_ROOT(已 resolve), 未 resolve 时回退远端读取
oc_data_root() {
    local ip="${1:-}"
    if [ -n "${DATA_ROOT:-}" ]; then printf '%s' "$DATA_ROOT"; return 0; fi
    if [ -n "$ip" ]; then node_data_root "$ip"; return 0; fi
    node_data_root "$(node_ip "$(node_names | head -1)")"
}
```

**这是审计 C-3 的收敛点** —— `panel/app.py` 从此不再拼 `/mnt/sd`。

---

## 3. 三种组网模式

### 3.1 模式定义

| 模式 | 节点间通信 | WireGuard | 依赖 LAN 互通 | 典型场景 |
| --- | --- | --- | --- | --- |
| `wireguard` | 全程走 wg0（10.8.0.0/24） | **必须** | 否（只要各自能到 Hub） | 跨网段、异地多机、LAN 不可信 |
| `lan` | 直接走 LAN（192.168.1.0/24） | **明确不装** | 是 | 同交换机、追求极简、不想引入隧道 |
| `mixed` | LAN 优先，跨网段走 wg | 装（Hub） | 部分 | 推荐默认：同 LAN 快，异地也能连 |

### 3.2 模式对全链路的影响矩阵

| 环节 | `wireguard` | `lan` | `mixed` |
| --- | --- | --- | --- |
| `bootstrap.sh` 装包 | 装 `wireguard-tools` | **不装**（从 `BASE_PKGS` 动态剔除） | 装 |
| `bootstrap.sh` 写 `install.conf` | 含 `WG_IP` | `WG_IP` 留空 | 含 `WG_IP` |
| `wireguard-setup.sh` | 生成 `wg0.conf` + 密钥 | **拒绝执行并说明原因** | 生成 |
| `deploy.sh` | 分发 wg 配置 | 跳过 `wireguard/` 目录 | 分发 |
| `health-check.sh` WireGuard 段 | 正常检查 | **输出 `SKIP 未启用 (mode=lan)`** | 正常检查 |
| `health-check.sh` 节点连通 | 优先测 wg IP | **只测 LAN IP** | 先 LAN 失败再试 wg |
| `firewall-recommend.sh` | 出 UDP 51820 规则 | **不出任何 WG 规则** | 出 UDP 51820 规则 |
| 面板拓扑文字 | "WireGuard Mesh VPN (10.8.0.0/24)" | "局域网直连 (192.168.1.0/24)" | "混合: LAN 直连 + WireGuard Mesh" |
| 面板节点卡片地址 | `ip (WG: wg_ip)` | **只显 `ip`，不显 WG 行** | 两句都显 |
| `health-check.sh` WG Hub | 绑 edge 角色 | — | 绑 edge 角色 |

### 3.3 连通性检查的分模式实现

`health-check.sh` 里新增一个单点函数，替代现在硬编码的 `WG_HUB_IP="$EDGE_IP"`：

```bash
# 返回该节点用于互连检查的首选地址 (可能多个, 按优先级)
# 由 lib-services.sh 提供, 不在 health-check.sh 里重复实现
probe_addr_for() {
    local n="$1"
    case "$(network_mode)" in
        lan)     printf '%s' "$(node_ip "$n")" ;;
        wireguard) printf '%s' "$(node_wg_ip "$n")" ;;
        mixed)   printf '%s' "$(node_ip "$n")" ;;   # 先 LAN, 失败再回退 wg
    esac
}
```

### 3.4 多模式下的"组网健康"判定

| 模式 | 健康条件 |
| --- | --- |
| `wireguard` | 所有节点 `wg show wg0` 成功，Hub peer 数 ≥ 节点数−1 |
| `lan` | 所有节点互相 `ping -c1 <LAN IP>` 成功 |
| `mixed` | LAN 互通 **或** wg 互通（二者任一即视为健康；两者皆失败才 FAIL） |

---

## 4. 面板改造

### 4.1 数据流（一条主线，不分叉）

```
inventory/nodes.yaml          network.mode
inventory/services.yaml       optional / default_enabled / install / provides
        │
        ▼
scripts/lib-services.sh       单一判定: network_mode / wg_enabled / service_installed / data_root
        │
        ▼
scripts/gen-panel-config.sh   渲染 panel/config.json
        │
        ▼
panel/config.json             cluster{network_mode, wg_enabled}
                              nodes[].data_root
                              nodes[].services[]{name, display, container, installed, optional, install, port}
        │
        ├──▶ panel/app.py     get_service_status() 先判 installed，未装直接返回 {installed:false}
        ├──▶ templates/index.html   拓扑区改为按 network_mode 渲染
        └──▶ static/js/app.js       三态渲染（运行 / 停止 / 未安装）
```

### 4.2 `panel/config.json` 新结构

```json
{
  "cluster_name": "OneCloud Cluster",
  "version": "1.6.0",
  "network_mode": "mixed",
  "network_mode_label": "混合组网 (LAN 直连 + WireGuard Mesh)",
  "wg_enabled": true,
  "wg_subnet": "10.8.0.0/24",
  "lan_subnet": "192.168.1.0/24",
  "update_interval": 10,
  "ssh_timeout": 3,
  "generated_by": "scripts/gen-panel-config.sh",
  "nodes": [
    {
      "name": "wk-edge-01",
      "display_name": "Edge Gateway",
      "role": "edge-gateway",
      "ip": "192.168.1.101",
      "wg_ip": "10.8.0.101",
      "hostname": "edge-01",
      "color": "#4CAF50",
      "data_root": "/mnt/sd",
      "services": [
        {"name":"cloudflared","display":"Cloudflare Tunnel","container":true,
         "installed":true,"optional":false,"install":"","port":0},
        {"name":"wireguard","display":"WireGuard","container":true,
         "installed":true,"optional":true,"install":"","port":51820},
        {"name":"clash","display":"Clash","container":false,
         "installed":true,"optional":true,"install":"","port":9090}
      ]
    }
  ]
}
```

**新增字段说明**：

| 层级 | 字段 | 类型 | 含义 |
| --- | --- | --- | --- |
| 顶层 | `network_mode` | string | 生效模式（`auto` 已解析） |
| 顶层 | `network_mode_label` | string | 中文展示名，前端直接用，不在前端拼 |
| 顶层 | `wg_enabled` | bool | 全局 WireGuard 开关 |
| 顶层 | `wg_subnet` / `lan_subnet` | string | 拓扑展示用；`lan` 模式下前端不渲染 wg 行 |
| 节点 | `data_root` | string | **该节点实际数据根**（面板执行 docker 命令时用，替代硬编码 `/mnt/sd`） |
| 服务 | `installed` | bool | 是否已安装（由 `lib-services.sh` 判定） |
| 服务 | `optional` | bool | 是否可选组件 |
| 服务 | `install` | string | 空 / `manual` / `external` |
| 服务 | `port` | int | 主端口（0 = 无），供前端显示与"未安装"时的占位 |

### 4.3 `panel/app.py` 改造点

| 位置 | 现状 | 改为 |
| --- | --- | --- |
| L17 CORS | 只放行 localhost/127.0.0.1 | 按时序计算实际来源 + `PANEL_CORS_ORIGINS` 追加 |
| L21-24 认证 | 裸 Basic-Auth，默认 `admin/changeme` | 登录页 + `session` cookie；首启随机密码并打印到日志 |
| L83 磁盘 | `df -h /mnt/sd` | `df -h <node.data_root>`（来自 config.json） |
| L163-173 `get_service_status` | 只返回 `running` | **先判 `installed`**：未安装直接 `{"installed": false}`，**不做任何 SSH 探测** |
| L217-220 `docker_*` | `/mnt/sd/srv/{node_name}` | `<node.data_root>/srv/{node_name}` |
| `collect_all_status()` | 无模式信息 | 透传 `network_mode` / `wg_enabled` / `network_mode_label` |
| 节点状态 | 无 `installed_count` | 增加 `services_installed` / `services_total` 统计 |

**`get_service_status` 新逻辑**（这是"不误报离线"的关键）：

```python
def get_service_status(ip, service):
    # 未安装: 不探测, 直接返回未安装态 (避免"没装却显示离线"误导用户)
    if not service.get("installed", True):
        return {
            "installed": False,
            "running": False,
            "type": "container" if service.get("container") else "native",
            "install": service.get("install", ""),
        }
    if service.get("container"):
        r = run_ssh(ip, f"docker inspect --format='{{{{.State.Status}}}}' {service['name']} 2>/dev/null")
        return {"installed": True, "running": r["ok"] and r["stdout"] == "running", "type": "container"}
    r = run_ssh(ip, f"systemctl is-active {service['name']} 2>/dev/null")
    if r["ok"] and r["stdout"] == "active":
        return {"installed": True, "running": True, "type": "systemd"}
    r2 = run_ssh(ip, f"pgrep -f '{service['name']}' 2>/dev/null")
    return {"installed": True, "running": r2["ok"] and r2["stdout"] != "", "type": "native"}
```

> 注意 `installed` 的**语义边界**：它是"**应当安装**"，不是"探测到进程存在"。
> 面板做的是"声明 vs 模式"判定，运行态由 `running` 表达。两者组合出三态：
> `installed=false` → 未安装；`installed=true & running=false` → 已装但停止；
> `installed=true & running=true` → 运行中。

### 4.4 前端三态渲染

`app.js` 的 `renderNode()` 服务项改为三态：

| `installed` | `running` | 状态点 | 文案 | 按钮 |
| --- | --- | --- | --- | --- |
| `false` + `install=""` | — | 灰色空心 | **未安装** | `[安装]`（提示需在节点执行 `install-services.sh`） |
| `false` + `install="manual"` | — | 灰色空心 | **待手动安装** | `[查看文档]` |
| `false` + `install="external"` | — | 灰色空心 | **未启用** | 无 |
| `true` | `false` | 红色 | 已停止 | `[▶ 启动]` `[📋 日志]` |
| `true` | `true` | 绿色 | 运行中 | `[↻ 重启]` `[■ 停止]` `[📋 日志]` |

关键实现（替换现有模板字符串）：

```javascript
function serviceState(svc) {
    if (!svc.installed) return { cls: "notinstalled", text: svc.install === "manual" ? "待手动安装"
                                     : svc.install === "external" ? "未启用" : "未安装" };
    return svc.running ? { cls: "running", text: "运行中" } : { cls: "stopped", text: "已停止" };
}
```

同时新增 **服务汇总行**：`服务 4/6 已装 · 3 运行`（而非现在的 `服务 3/6`，那个分母把未安装的也算进去了 —— 这是"不装 WG 时显示错误"的直接原因）。

### 4.5 拓扑区按模式渲染

`index.html` 的硬编码行 `<div class="topo-mesh">WireGuard Mesh VPN (10.8.0.0/24)</div>`
改为占位元素 `id="topoMode"`，由 `app.js` 按 `network_mode` 填充：

| 模式 | 拓扑文字 | 视觉 |
| --- | --- | --- |
| `wireguard` | `WireGuard Mesh VPN (10.8.0.0/24)` | 节点间连虚线（隧道） |
| `lan` | `局域网直连 (192.168.1.0/24)` | 节点间连实线（LAN），**不出现 WG 字样** |
| `mixed` | `混合组网：LAN 直连优先 + WireGuard Mesh (10.8.0.0/24)` | 实线 + 虚线并存 |

节点卡片地址行同步调整：

```javascript
// lan 模式不展示 WG 行, 避免"没装却显示 10.8.0.x"的误导
const addr = data.wg_enabled
    ? `${node.name} · ${node.ip} (WG: ${node.wg_ip})`
    : `${node.name} · ${node.ip}`;
```

### 4.6 面板 API 变更（向后兼容）

| 路径 | 变更 |
| --- | --- |
| `GET /api/status` | 响应新增 `network_mode` / `network_mode_label` / `wg_enabled`；`nodes[].services[]` 新增 `installed` / `optional` / `install`；`nodes[]` 新增 `data_root` / `services_installed` |
| `POST /api/service/<node>/<svc>/<action>` | 未安装服务返回 `409 {"error":"服务未安装"}`，**不再尝试 `systemctl start` 一个不存在的单元** |
| `GET /login` / `POST /login` / `POST /logout` | 新增（替代裸 Basic-Auth） |
| `GET /api/network` | 新增：返回模式与各节点探测地址，供拓扑与排障使用 |

---

## 5. 各脚本改造清单

### 5.1 新增

| 文件 | 说明 |
| --- | --- |
| `scripts/lib-services.sh` | 安装态/模式/数据根单一判定（§2） |

### 5.2 修改

| 文件 | 改动 | 对应审计项 |
| --- | --- | --- |
| `inventory/services.yaml` | 加 `optional` / `default_enabled` / `install` / `provides` | H-1, H-3 |
| `inventory/nodes.yaml` | `network:` 加 `mode` | H-3 |
| `scripts/lib-nodes.sh` | `_parse_nodes_yaml` 捕获 `network.mode` → `NET_MODE`；新增 `node_data_root_for()` 便于本机/远端统一 | H-3, C-3 |
| `scripts/gen-panel-config.sh` | source `lib-services.sh`；渲染 `network_mode*` / `wg_enabled` / `data_root` / 服务 `installed` 等新字段 | H-3 |
| `scripts/bootstrap.sh` | ① `BASE_PKGS` 按模式动态剔除 `wireguard-tools`；② `SVC_TREE` 按模式剔除 `wireguard/config`；③ `install.conf` 增加 `NETWORK_MODE` / `WG_ENABLED`；④ 参数加 `--net-mode <auto\|wireguard\|lan\|mixed>` | H-2, H-3 |
| `scripts/wireguard-setup.sh` | 开头判 `wg_enabled`，为 0 时打印原因并 `exit 0`（非错误退出） | H-2 |
| `scripts/wireguard-setup.sh` | `HUB_NODE` 改为从清单 `wireguard.node` 反查（`node_of_service wireguard`），角色名只作回退 | H-2 |
| `scripts/health-check.sh` | ① WG 段未启用 → `SKIP`；② `check_container`/`check_port` 对未安装服务跳过；③ `probe_addr_for` 按模式取地址；④ `$container` 加引号；⑤ `check_port` 端口匹配改正则（修 L-3/L-4） | H-2, L-3, L-4 |
| `scripts/firewall-recommend.sh` | `wg_enabled=0` 时不生成 UDP 51820 规则与 Hub 转发段 | H-2 |
| `scripts/install-services.sh` | **source `lib-nodes.sh` + `lib-services.sh`**；`start_compose` 用 `service_data_dir`/`node_data_dir`；`install_verysync` 改为诚实提示 + 支持 `manual` 语义；新增 `--list-installed` | **C-3**, H-1 |
| `scripts/deploy.sh` | 分发时跳过未安装服务的目录（`wireguard/` 等） | H-3 |
| `scripts/setup.sh` | `DATA_DIR` 与 `install_path_for srv` 语义对齐为 `node_data_dir`；未安装服务不生成单元 | C-3 |
| `init/init.sh` | `menu_wireguard` 在 `lan` 模式下提示"当前为纯 LAN 模式，未启用 WireGuard"并允许切模式；`menu_services` 增加"查看安装状态"入口 | H-3 |
| `panel/app.py` | §4.3 全部改造 | **C-1**, **C-2**, C-3, M-1, H-3 |
| `panel/templates/index.html` | 拓扑占位 + 登录页模板 | C-1, H-3 |
| `panel/static/js/app.js` | 三态渲染 + 汇总行 + 模式拓扑 + `credentials: "same-origin"` | **C-1**, H-3 |
| `panel/static/css/style.css` | 新增 `.status-dot.notinstalled` 灰色空心样式、登录页样式 | H-3 |

### 5.3 节点侧硬编码路径（C-3 的 11 处），模板中用占位符 `__DATA_ROOT__`：

| 文件 | 现写法 | 改法 |
| --- | --- | --- |
| `node-wk-edge-01/clash/install-service.sh` | `ExecStart=/usr/local/bin/mihomo -d /mnt/sd/edge-01/clash` | `ExecStart=... -d __DATA_ROOT__/srv/wk-edge-01/clash`，安装脚本 `sed` 替换 |
| `node-wk-iot-02/xiaomusic/install-service.sh` | `-c /mnt/sd/iot-02/xiaomusic/config.json`、`WorkingDirectory=...` | 同上 |
| `node-wk-iot-02/xiaomusic/config.json` | 3 处路径 | 安装时生成，不再随仓库分发固定路径 |
| `node-wk-storage-03/verysync/config.yaml` | 4 处路径 | 同上 |
| `node-wk*/clash/install-binary.sh`、`xiaomusic/install.sh` | 用户提示文案 | 改为变量化提示 |

> 实现方式：`install-services.sh` 新增 `render_unit()`，读模板 → `sed "s|__DATA_ROOT__|$DATA_ROOT|g"` → 写 `/etc/systemd/system/`。
> 同时保留兼容：若模板仍是绝对 `/mnt/sd`，安装时告警提示"该文件未模板化"。

---

## 6. 边界情况与行为约定

| # | 场景 | 约定行为 |
| --- | --- | --- |
| 1 | `mode=lan` 但 `services.yaml` 仍有 `wireguard` | `wg_enabled()=0`，全链路视作未安装；`bootstrap` 不装 `wireguard-tools`；面板不渲染 WG 行 |
| 2 | `mode=wireguard` 但清单里 `wireguard` 被删 | 启动前校验：`require_wg` 失败并提示"模式要求 WireGuard，但清单未声明" |
| 3 | `mode=auto` 且无任何节点声明 wireguard | 解析为 `lan` |
| 4 | `mode=auto` 且 wireguard 存在但 `default_enabled: false` | 解析为 `lan`（**未启用**）；用户显式 `--net-mode mixed` 才启用 |
| 5 | 不装 WG，`health-check.sh` | WG 段输出 `[SKIP] WireGuard 未启用 (mode=lan, wg_enabled=0)`，不计入 FAIL |
| 6 | 不装 WG，面板 | 拓扑显"局域网直连"；节点卡无 WG 行；`wireguard` 服务项显示"未安装"灰点 |
| 7 | 不装 WG，`wireguard-setup.sh` | `[INFO] 当前组网模式为 lan, 未启用 WireGuard; 如需启用: ./scripts/bootstrap.sh --net-mode mixed`，`exit 0` |
| 8 | 不装 WG，`firewall-recommend.sh` | 输出不含 UDP 51820、不含 `FORWARD -i wg0` / `MASQUERADE` 段 |
| 9 | 服务标 `manual`（verysync） | 面板显"待手动安装"；`install-services.sh verysync` 打印下载指引；`health-check` 不计 FAIL |
| 10 | `mode=wireguard` 时 LAN 不通 | 仍健康（WG 不依赖 LAN）；但 `bootstrap` 首次配网必须先经 LAN 完成，文档需注明 |
| 11 | 切模式后配置未刷新 | `gen-panel-config.sh` + `sync-panel-config.sh` 重跑；`init.sh` 切模式后自动调用 |
| 12 | `install.conf` 缺失 `NETWORK_MODE`（老节点） | 回退读清单；再取不到按 `mixed` 处理并告警 |
| 13 | 面板访问一个未安装服务的启动接口 | 返回 `409` + 明确文案，不执行 `systemctl start` |
| 14 | 所有节点离线 | 面板显示"全部离线"但**仍显示每个服务的安装态**（安装态来自清单，不依赖 SSH） |

---

## 7. 测试计划

### 7.1 新增测试组（第 33 / 34 / 35 组）

| 组 | 名称 | 项数（估） | 覆盖 |
| --- | --- | --- | --- |
| 33 | **组件安装态模型** | ~25 | `lib-services.sh` 全部函数；`optional`/`install` 解析；`service_installed` 真值表；`auto` 模式解析；`mode=lan` 强制关闭 WG |
| 34 | **三种组网模式全链路** | ~30 | 模式 × 脚本矩阵：bootstrap 装包清单、`install.conf` 字段、wireguard-setup 拒绝、health-check SKIP、firewall 无 WG 规则、面板字段 |
| 35 | **面板安装态与数据根** | ~20 | `config.json` 新字段存在且类型正确；`app.py` 未安装不探测；`app.js` 三态渲染契约；**所有 `fetch` 带 `credentials`**；CORS origins 含实际访问地址 |

### 7.2 必须加的**取反式断言**（针对审计暴露的盲区）

```
① 全库不得出现字面量 "/mnt/sd/srv"
② 全库不得出现 /mnt/sd/<短节点名> 形式的硬编码 (节点侧配置与单元模板)
③ app.js 中每个 fetch( 必须出现在含 credentials 的调用内
④ require_auth 覆盖的路由集合 == 前端实际调用集合 (双向比对)
⑤ container: false 的服务必须在 install-services.sh 有 case 分支或标 install: manual
⑥ mode=lan 时生成的防火墙建议不得含 51820
⑦ 任何脚本使用远程路径前必须有 node_data_root / oc_data_root 调用
```

### 7.3 回归要求

现有 32 组 555 项**必须全部保持通过**。特别注意：

- 第 11 组（服务清单五方一致性）：新增 `lib-services.sh` 后需把 `services.yaml` 解析扩展到
  `optional` / `install`，并把它加入"五方"比对（实际变成"六方"）。
- 第 19 组（版本声明一致）：版本升到 `1.6.0` 后，**五处同步**（README 顶部、`panel/config.json`、
  `panel/app.py` 降级默认、`gen-panel-config.sh` 的 `VERSION` 默认、`panel/README.md` 示例）都要改。
- 第 27 组（部署零防火墙写入）：`firewall-recommend.sh` 改模式分支后需重跑。

---

## 8. 交付顺序（分四批，每批独立可验证）

| 批次 | 内容 | 版本 | 依赖 |
| --- | --- | --- | --- |
| **P0** | 面板可用性：C-1 登录会话 + C-2 CORS + 未安装不探测 + `data_root` 替代 `/mnt/sd` | v1.5.6 | 无（可立即做，与模式设计解耦） |
| **P1** | 单一真相层：`lib-services.sh` + 清单新字段 + `gen-panel-config.sh` + `health-check.sh` SKIP | v1.6.0-a | P0 的面板字段结构 |
| **P2** | 数据根收敛：C-3 全部（`install-services.sh` source 库、节点侧 11 处模板化、`setup.sh` 语义对齐） | v1.6.0-b | P1 |
| **P3** | 三模式全链路 + 面板三态渲染 + 拓扑模式化 + 测试组 33/34/35 | **v1.6.0** | P1, P2 |

> **建议 P0 先落地**：面板 C-1/C-2 是"开箱即不可用"，与本次模式设计无耦合，
> 单独发一个补丁版本最安全，也让 P1–P3 的验收有可用的观测界面。

---

## 9. 风险与权衡

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| `installed` 语义被误解为"探测到进程" | 面板显示与实际不符 | 文档与代码注释明确：`installed` = "应当安装"（声明+模式判定），运行态由 `running` 表达 |
| `mode=lan` 但物理上 LAN 不通 | 集群断联，且无 WG 兜底 | `bootstrap` 配网前风险预检（已有）追加"lan 模式要求节点互通性验证"，失败即告警 |
| 模板化节点侧单元引入渲染错误 | 服务起不来 | `render_unit()` 后校验：渲染结果不含 `__DATA_ROOT__`、路径存在、`systemd-analyze verify`（可用时） |
| 老集群（无 `NETWORK_MODE`）升级 | 行为突变 | 缺失即回退 `mixed`（等价于现状），不改变老环境行为 |
| 面板登录会话引入 CSRF 面 | 安全 | 会话 cookie 设 `SameSite=Lax` + `HttpOnly`；状态变更接口要求自定义头 `X-Requested-With` |
| 版本 5 处同步漏改 | 第 19 组失败 | 用 `scripts/gen-panel-config.sh` 重跑 + 第 19 组测试兜底 |

---

## 10. 与审计报告的对应关系

| 审计项 | 本方案解决位置 |
| --- | --- |
| **C-1** 前端无鉴权 → 401 | §4.3 登录会话 + §5.2 `app.js` `credentials`；§7.2 断言③④ |
| **C-2** CORS 无 LAN 来源 | §4.3 L17 改造；§7.2 断言④ |
| **C-3** 数据根三套写法 | §2.2 `service_data_dir` 单点 + §5.2 `install-services.sh`/`setup.sh` + §5.3 节点侧 11 处；§7.2 断言①②⑦ |
| **H-1** verysync 无安装实现 | §1.1 `install: manual` + §6 边界 9；§7.2 断言⑤ |
| **H-2** health-check 无条件查 WG | §3.2 矩阵 + §3.3 `probe_addr_for` + §5.2；§7.2 断言⑥ |
| **H-3** 无安装态模型 | §1 清单字段 + §2 `lib-services.sh` + §4 面板全链路 |
| M-1 面板硬编码 `/mnt/sd` | §4.3 L83 用 `data_root` |
| M-2/M-3/M-4 文档过期 | 各批次末尾随版本更新（`docs/` + README 计数与目录树） |
| L-3/L-4 `health-check.sh` 引号与端口匹配 | §5.2 health-check 改造 |
| L-1/L-2 `restore.sh` 残留 | 随 P2 顺手清理 |

---

## 11. 验收标准

**必须同时满足以下全部条件才算完成：**

1. `mode=lan` 且清单无 `wireguard` → `health-check.sh` 输出 **0 个 FAIL**，WG 段为 SKIP
2. 同上环境 → `panel` 页面拓扑显示"局域网直连"，节点卡**无 WG 行**，`wireguard` 服务项为"未安装"灰点
3. 同上环境 → `firewall-recommend.sh` 输出**不含** 51820 与任何 `wg0` 规则
4. 同上环境 → 面板面板所有功能可用（登录、刷新、服务启停、exec），**无 401 / 无 CORS 报错**
5. `mode=mixed` → 拓扑显示混合，WG 服务为运行中，`health-check.sh` LAN 与 WG 均检查
6. `mode=wireguard` 且清单含 `wireguard` → 同 5，但连通性优先走 wg IP
7. 任意模式下，`--no-sd` 节点（数据根 `/opt/onecloud`）的原生服务安装到 `/opt/onecloud/srv/<节点>/<服务>`
8. `verysync` 在面板显示"待手动安装"而非"离线"
9. 现有 32 组 555 项 + 新增 33/34/35 组全部通过
10. 版本 `1.6.0` 五处声明一致

---

## 11.1 实施与验收结果（2026-09-18 完成）

**结论：设计方案已全部落地，验收标准 1–10 全部满足。**

### 验收对照

| # | 验收项 | 结果 | 证据 |
|---|--------|------|------|
| 1 | lan 模式 health-check 0 FAIL、WG 段 SKIP | ✅ | 未安装服务走 SKIP 分支，`manual` 显示"待手动安装" |
| 2 | 面板拓扑"局域网直连"、无 WG 行、灰点未安装 | ✅ | `.status-dot.notinstalled` + `topoMode` + `serviceState()` 三态 |
| 3 | lan 模式防火墙不含 51820 / wg0 规则 | ✅ | 全部节点 DSL 无 51820；正文仅剩"本清单不含…"提示句 |
| 4 | 面板全功能可用、无 401 / 无 CORS 报错 | ✅ | `_panel_e2e.py` 36 项全过（登录/会话/CORS/CSRF/409） |
| 5 | mixed → 拓扑混合、WG 运行中、LAN+WG 均检查 | ✅ | DSL 含 51820，deploy 保留 `wireguard/config` |
| 6 | wireguard 模式同上且优先 wg IP | ✅ | `probe_addr_for` 优先 wg IP，回退 LAN |
| 7 | 无 SD 节点数据根为 `/opt/onecloud` | ✅ | `oc_data_root` 统一拼接，7 条反向断言盯住 |
| 8 | verysync 显示"待手动安装" | ✅ | `install: manual` → `installed=false`，SSH 探测被短路 |
| 9 | 原有 32 组 + 新增 33/34/35 全通过 | ✅ | 33 组 56 项 / 34 组 60 项 / 35 组 55 项，全 0 FAIL |
| 10 | 版本 1.6.0 声明一致 | ✅ | 6 处（README 两处）全部 `1.6.0` |

### 实施期新发现并修复的 8 个缺陷

方案本身没问题，但落地过程中由**测试反向断言**抓出 8 个真实缺陷（含 1 个 P0）。
完整清单与根因见 `docs/to-fix.md` 第六节。摘要：

1. **P0** lan 模式防火墙仍放行 UDP 51820（`parse_service_ports` 绕过模式门禁）
2. P1 `services_status_table` 空字段被 `IFS=$'\t' read` 折叠 → 「安装方式」列显示成端口号
3. P1 `list-installed` 中文表头 `printf %-Ns` 按字节填充 → 列错位
4. P1 `deploy.sh` 预建目录丢失花括号展开形式，破坏「单条 mkdir + 可静态核对」契约
5. P2 `panel/app.py` 缺 `_SAFE_SVC_NAME` 定义（服务名直拼 shell = 命令执行入口）
6. P2 模式标签双重括号 `lan (局域网直连 (…))`
7. **P1** `gen-panel-config.sh` 逐节点 SSH 探测数据根 → 生成静态配置却白等 `ConnectTimeout` 秒级/节点
8. **P1** `_svc_first_port` 裸 `${!arr}` 展开在 `set -u` 下报 unbound，被 `2>/dev/null` 吞掉 →
   **所有 `ports: [...]` 形式的容器服务端口静默退化为 0**（18 个服务只有 2 个非零）

第 7/8 两项是第二轮排查第 32 组超时时顺线发现的，已完成修复并新增第 36 组 20 项回归断言。

### 性能提示（Git Bash on Windows）

`services_status_table` 原本单次调用 200s（现已加四层缓存降至 105s）。
剩余耗时的本质是本环境**函数调用开销 ~0.55s/次**，而该表有 ~110 次调用 ——
**不是代码缺陷**，Linux 真机上为亚秒级。测试已改为单次调用 + 同份输出多项断言。

`gen-panel-config.sh` 由 160s 优化到 104s：移除逐节点 SSH（-27s）、热路径去 `$( )` 子 shell、
`service_field` 改关联数组直查。**残余 ~104s 同样是本环境 fork 开销**，且该脚本每次部署
只在控制端跑一次，不再继续优化。

---

## 12. 可行性验证（设计阶段实测）

以下三项在本机对现有代码做了探针验证，确认方案不需要重写解析器：

| 验证项 | 方法 | 结果 |
| --- | --- | --- |
| `network.mode` 能否被现有解析器捕获 | `source scripts/lib-nodes.sh` 后检查 `NET_DNS` / `NET_WG_PORT` 有值、`NET_MODE` 未定义；`awk` 探针列出 `network:` 段全部被匹配的键 | 现有 6 个键全部命中；**新增 `mode:` 会自动落入同一匹配规则**，解析器无需改动，只需在 `load_nodes` 里把 `NET_MODE` 取值行加一行 |
| `services.yaml` 新字段是否破坏现有解析 | 用 `sed` 给所有服务注入 `optional:` / `default_enabled:` 后跑现有 awk 解析规则 | 新字段被正确识别并归到对应服务名下，**不影响 `node:` / `container:` 的现有解析** |
| 版本声明同步位置 | `grep -n "1.5.5"` 覆盖 5 个文件 | 确认 6 处：`README.md`（第 5、495 行）、`panel/config.json:3`、`panel/app.py:60`、`scripts/gen-panel-config.sh:21`、`panel/README.md:148`。注意 README 有**两处**，升版本时容易漏第 495 行的变更说明标题 |

> 结论：本方案的清单层改动是**纯增量**的，不破坏现有 YAML 结构，老清单不改也能继续跑（`optional` 默认 `false`、`mode` 默认 `mixed`）。
