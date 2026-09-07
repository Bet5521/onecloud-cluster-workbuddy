# 📋 inventory/ — 集群清单

集群的**单一数据源**。所有运维脚本的节点 IP、主机名、WireGuard 地址、网络参数均从此目录读取。

---

## 文件说明

| 文件 | 用途 | 是否入库 |
|------|------|---------|
| `nodes.yaml` | 节点默认清单（3 个节点 + 网络参数） | ✅ 是 |
| `nodes.local.yaml.example` | 本地覆盖模板 | ✅ 是（模板） |
| `nodes.local.yaml` | 本地覆盖文件（实际覆盖） | ❌ 已 gitignore |
| `services.yaml` | 服务注册表（18 个服务的镜像/端口/节点绑定） | ✅ 是 |

---

## 三层覆盖机制

优先级从高到低：

| 层级 | 方式 | 适用场景 |
|------|------|---------|
| 1. 环境变量 | `ONECLOUD_<节点大写>_<字段>=值` | 临时覆盖 / CI 注入 |
| 2. 本地覆盖文件 | `inventory/nodes.local.yaml` | 不想改动入库的默认清单 |
| 3. 默认清单 | `inventory/nodes.yaml` | 集群的基准定义 |

### 环境变量命名规则

节点名转大写、`-` 换 `_`，字段为 `IP` / `HOSTNAME` / `WG_IP` / `ROLE`：

```bash
# 示例：把 edge 节点 IP 改为 10.20.30.41
export ONECLOUD_WK_EDGE_01_IP=10.20.30.41
```

网络参数：`ONECLOUD_GATEWAY` / `ONECLOUD_DNS` / `ONECLOUD_DOMAIN` / `ONECLOUD_WG_PORT` / `ONECLOUD_LAN_PREFIX` / `ONECLOUD_WG_SUBNET`

---

## 快速自定义

```bash
# 方式一：环境变量（临时）
export ONECLOUD_WK_EDGE_01_IP=10.20.30.41
./scripts/bootstrap.sh --node wk-edge-01 --yes

# 方式二：本地覆盖文件（推荐长期使用）
cp nodes.local.yaml.example nodes.local.yaml
vim nodes.local.yaml   # 填入你的 IP/主机名
```

改完 IP 后重新渲染：

```bash
./scripts/gen-panel-config.sh   # 面板配置
./scripts/gen-node-env.sh       # 各节点 .env
```

---

## nodes.yaml 结构

```yaml
nodes:
  - name: wk-edge-01
    hostname: edge-01
    ip: 192.168.1.101
    wg_ip: 10.8.0.101
    role: edge-gateway

network:
  gateway: 192.168.1.1
  dns: 1.1.1.1
  domain: yourdomain.com
  lan_subnet: 192.168.1.0/24
  wg_subnet: 10.8.0.0/24
  wg_port: 51820
```

## services.yaml 结构

```yaml
cloudflared:
  image: cloudflare/cloudflared:latest
  node: wk-edge-01
  ports: []
  description: "Cloudflare Tunnel 客户端"
```

每个服务定义 `image`（镜像）、`node`（绑定节点）、`ports`（端口列表）、`description`（描述）。
