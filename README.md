# 🛠️ OneCloud 集群部署与运维指南

> 基于玩客云 WS1608 (Amlogic S805, ARMv7, 1GB RAM) 多节点组建的家庭服务集群

**当前版本: v1.1.0**

---

## 📋 项目概述

### 硬件规格

| 项目 | 规格 |
|------|------|
| CPU | Amlogic S805 四核 1.5GHz |
| 架构 | ARMv7 (32-bit, armhf) |
| 内存 | 1GB DDR3（每个节点挂载 2GB swap） |
| 存储 | 8GB eMMC（系统）+ 128GB SD 卡（Docker 数据） |
| 网络 | 100Mbps Ethernet |

### 节点角色划分

| 节点 | 主机名 | IP | WireGuard IP | 角色 | 服务 |
|------|--------|----|--------------|------|------|
| NODE-01 | wk-edge-01 | 192.168.1.101 | 10.8.0.101 | Edge Gateway | cloudflared, AdGuard Home, WireGuard, Memos, mihomo(Clash), Panel |
| NODE-02 | wk-iot-02 | 192.168.1.102 | 10.8.0.102 | IoT Core | Home Assistant, Piwigo, xiaomusic, migpt, Typecho |
| NODE-03 | wk-storage-03 | 192.168.1.103 | 10.8.0.103 | Storage & Sync | Syncthing, aria2/AriaNg, CUPS/cups-web, verysync, Gitea |

### 网络架构

```
外部网络
    │
    ▼
Cloudflare Zero Trust (零信任访问)
    │
    ▼
wk-edge-01 (192.168.1.101)
  ├── AdGuard Home (家庭 DNS)
  ├── WireGuard Server (集群互联)
  └── Clash (流量代理)
    │
    ▼ WireGuard Mesh (10.8.0.0/24)
    ├── wk-iot-02 (10.8.0.102)
    └── wk-storage-03 (10.8.0.103)
```

### 服务端口矩阵

| 服务 | 节点 | 端口 | 协议 | 公网访问 |
|------|------|------|------|---------|
| AdGuard Home | edge-01 | 3000 / 53 | TCP / UDP | ✅ CF Tunnel |
| WireGuard | edge-01 | 51820 | UDP | ✅ CF Tunnel |
| Memos | edge-01 | 5230 | TCP | ✅ CF Tunnel |
| Clash Web | edge-01 | 9090 | TCP | ❌ 内网 |
| Cluster Panel | edge-01 | 9000 | TCP | ✅ CF Tunnel |
| Home Assistant | iot-02 | 8123 | TCP | ✅ CF Tunnel |
| Piwigo | iot-02 | 8080 | TCP | ✅ CF Tunnel |
| Typecho | iot-02 | 8083 | TCP | ✅ CF Tunnel |
| xiaomusic | iot-02 | 8081 | TCP | ❌ 内网 |
| migpt | iot-02 | 8082 | TCP | ❌ 内网 |
| Syncthing | storage-03 | 8384 / 22000 | TCP | ❌ 内网 |
| aria2 | storage-03 | 6800 | TCP | ❌ 内网 |
| AriaNg | storage-03 | 6880 | TCP | ❌ 内网 |
| CUPS | storage-03 | 631 | TCP | ❌ 内网 |
| CUPS Web | storage-03 | 632 | TCP | ❌ 内网 |
| Gitea | storage-03 | 3000 | TCP | ✅ CF Tunnel |
| Gitea SSH | storage-03 | 222 | TCP | ❌ 内网 |
| verysync | storage-03 | 19900 | TCP | ❌ 内网 |

---

## 📁 目录结构

```
onecloud-cluster/
├── README.md
├── .gitignore
├── docs/                     # 文档
│   ├── requirements.md       # 需求分析、功能清单
│   ├── architecture.md       # 架构设计、网络拓扑、存储规划
│   ├── topology.md           # 可视化拓扑图 (Mermaid + ASCII)
│   ├── operations.md         # 运维手册
│   └── cloudflare-setup.md   # Cloudflare Tunnel 配置
├── inventory/                # 集群清单 (单一数据源)
│   ├── nodes.yaml
│   └── services.yaml
├── node-wk-edge-01/          # NODE-01 Edge Gateway 配置
├── node-wk-iot-02/           # NODE-02 IoT Core 配置
├── node-wk-storage-03/       # NODE-03 Storage & Sync 配置
├── panel/                    # Flask 集群控制面板
├── scripts/                  # 运维脚本
└── test_validate.py          # 集群配置/脚本验证套件
```

---

## 🚀 快速开始

### 1. 初始化节点

在每个节点上执行一次（支持非交互参数，便于批量/自动化）：

```bash
./scripts/bootstrap.sh --node wk-edge-01    --ip 192.168.1.101 --hostname edge-01    --yes
./scripts/bootstrap.sh --node wk-iot-02     --ip 192.168.1.102 --hostname iot-02     --yes
./scripts/bootstrap.sh --node wk-storage-03 --ip 192.168.1.103 --hostname storage-03 --yes
```

> 不带参数运行时会进入交互式提问；非交互环境（cron / CI）请务必加 `--yes`。

### 2. 生成 WireGuard 配置

```bash
./scripts/wireguard-setup.sh                    # 生成全部节点密钥与 wg0.conf
./scripts/wireguard-setup.sh list               # 查看已登记节点
./scripts/wireguard-setup.sh add peer wk-backup-04 192.168.1.104 10.8.0.104
```

生成的配置写入各 `node-<名称>/wireguard/wg0.conf`。该文件**含私钥，已被 `.gitignore` 忽略**，不会入库。

### 3. 分发配置并启动

```bash
./scripts/deploy.sh                              # 分发到所有节点
./scripts/deploy.sh -n wk-edge-01               # 仅分发到指定节点
./scripts/deploy.sh --exec "docker-compose up -d"   # 分发后远程启动服务
```

### 4. 安装服务（可选）

单节点交互式安装（端口冲突检测 + 多选批量 + U 盘/SD 卡挂载）：

```bash
sudo bash scripts/setup.sh
```

或按角色/按服务安装：

```bash
./scripts/install-services.sh mihomo      # 原生二进制: mihomo / xiaomusic / migpt / verysync
./scripts/install-services.sh edge        # Docker: edge / iot / storage
```

### 5. 启动控制面板

```bash
cd panel && pip3 install -r requirements.txt && python3 app.py
# 访问 http://<edge节点IP>:9000
```

面板已启用 HTTP Basic Auth 与命令白名单，默认账号 `admin` / `changeme`，
请通过环境变量覆盖：

```bash
PANEL_USER=admin PANEL_PASS='强密码' PANEL_PORT=9000 python3 app.py
```

> 面板展示的节点 IP/主机名由 `panel/config.json` 驱动。该文件由
> `scripts/gen-panel-config.sh` 从 `inventory/nodes.yaml` 生成，IP 改动后
> 重新执行一次即可同步，无需手改。

---

## 🎛️ 节点 IP / 主机名自定义

**所有节点的 IP、主机名、WireGuard 地址、网段参数都不再写死在脚本里**，
统一由 `inventory/nodes.yaml` 提供，并支持三层覆盖（优先级从高到低）：

| 层级 | 方式 | 适用场景 |
|------|------|---------|
| 1. 环境变量 | `ONECLOUD_<节点大写>_<字段>=值` | 临时覆盖 / CI 注入 |
| 2. 本地覆盖文件 | `inventory/nodes.local.yaml`（参考 `nodes.local.yaml.example`） | 不想改动入库的默认清单 |
| 3. 默认清单 | `inventory/nodes.yaml` | 集群的基准定义 |

环境变量命名规则：节点名转大写、`-` 换 `_`，字段为 `IP` / `HOSTNAME` / `WG_IP` / `ROLE`；
网络参数用 `ONECLOUD_GATEWAY` / `ONECLOUD_DNS` / `ONECLOUD_DOMAIN` / `ONECLOUD_WG_PORT` / `ONECLOUD_LAN_PREFIX` / `ONECLOUD_WG_SUBNET`。

示例 —— 把 edge 节点部署到 `10.20.30.41`、主机名 `edge-hk`：

```bash
# 方式一: 环境变量 (对所有脚本全局生效)
export ONECLOUD_WK_EDGE_01_IP=10.20.30.41
export ONECLOUD_WK_EDGE_01_HOSTNAME=edge-hk
./scripts/bootstrap.sh --node wk-edge-01 --yes
./scripts/deploy.sh -t        # 巡检目标自动变成新 IP

# 方式二: 本地覆盖文件 (推荐长期使用)
cp inventory/nodes.local.yaml.example inventory/nodes.local.yaml
vim inventory/nodes.local.yaml   # 填入你的 IP/主机名, 该文件已被 gitignore
```

要点：

- `bootstrap.sh` 对已登记节点自动取清单中的 IP/主机名作为默认值，命令行
  `--ip/--hostname` 可再覆盖；网关、DNS、`/etc/hosts` 全部参数化
- 所有运维脚本（deploy / update-all / health-check / backup / restore /
  wireguard-setup / setup / install-services）均从 `scripts/lib-nodes.sh`
  动态读取节点，**功能脚本中无任何硬编码 IP**
- `backup.sh` / `restore.sh` 的备份目录可用 `BACKUP_DIR` 环境变量自定义
- 新增节点：写进 `nodes.local.yaml` 即被所有脚本识别，无需改代码

---

## 🔧 运维脚本

| 脚本 | 作用 | 常用用法 |
|------|------|---------|
| `lib-nodes.sh` | 节点清单库（单一数据源，供所有脚本 source） | 被其他脚本引用 |
| `gen-panel-config.sh` | 从清单生成 `panel/config.json` | 直接运行 |
| `bootstrap.sh` | 新节点初始化（主机名/源/swap/SD卡/Docker/静态IP） | `--node <名> [--ip <IP>] --yes` |
| `setup.sh` | 统一安装（端口检测 + 多选批量安装 + 磁盘挂载） | `sudo bash setup.sh` |
| `wireguard-setup.sh` | WireGuard mesh 配置生成 | `gen` / `add peer` / `list` |
| `deploy.sh` | rsync 分发配置到各节点 | `-n <节点>` / `--exec <命令>` / `-t` / `-d` |
| `install-services.sh` | 安装原生二进制或启动节点容器 | `mihomo` / `edge` / `all-native` |
| `health-check.sh` | 集群健康巡检（SSH/容器/端口/负载/OOM） | 直接运行 |
| `backup.sh` | 备份配置与数据（`BACKUP_DIR` 可自定义） | `all` / `config` / `node <名>` / `service <名>` |
| `restore.sh` | 从备份恢复 | `<备份ID> <目标>` 或 `<备份ID> service <名>` |
| `update-all.sh` | 批量更新镜像/系统包（不升 Docker Engine） | `-d` / `-s` / `-a` / `-n <节点>` |

所有脚本均支持 `-h` / `--help` 查看完整用法。

---

## ✅ 功能验证

项目自带验证套件，覆盖配置完整性、脚本语法、节点映射、服务一致性、
**文档化 CLI 接口契约**与 **IP/主机名可自定义性**（共 184 项）：

```bash
python3 test_validate.py
```

输出示例：

```
  总计: 184 项
  通过: 184
  失败: 0
  警告: 0
```

报告同时写入 `test_report.txt`（已被 gitignore）。

---

## 📚 详细文档

| 文档 | 内容 |
|------|------|
| [docs/requirements.md](docs/requirements.md) | 需求分析、功能清单 |
| [docs/architecture.md](docs/architecture.md) | 架构设计、网络拓扑、存储与内存规划 |
| [docs/topology.md](docs/topology.md) | 可视化拓扑图 (Mermaid + ASCII) |
| [docs/operations.md](docs/operations.md) | 运维手册（备份/恢复/更新/故障排查） |
| [docs/cloudflare-setup.md](docs/cloudflare-setup.md) | Cloudflare Tunnel 配置 |

---

## 🎛️ v1.2.0 变更说明

### 节点 IP / 主机名全面可自定义

此前节点 IP（`192.168.1.101-103`）与主机名散落硬编码在 9 个脚本与面板配置中，
换网段必须改代码。本版本建立统一节点清单机制：

- **新增 `scripts/lib-nodes.sh`**：从 `inventory/nodes.yaml` 解析节点
  IP / 主机名 / WG 地址 / 角色与网络参数（网关、DNS、域名、WG 端口、子网），
  提供三层覆盖：环境变量 > `inventory/nodes.local.yaml` > 默认清单
- **`inventory/nodes.yaml`** 为每个节点显式增加 `hostname` 字段（默认
  `edge-01` / `iot-02` / `storage-03`），并补充 `network` 段完整参数
- **9 个运维脚本全部去硬编码**：`bootstrap.sh`（静态 IP/网关/DNS/hosts
  参数化，已登记节点自动取默认值）、`deploy.sh`、`update-all.sh`、
  `health-check.sh`（31 处）、`backup.sh`、`restore.sh`、`setup.sh`、
  `wireguard-setup.sh`、`install-services.sh`
- **新增 `scripts/gen-panel-config.sh`**：`panel/config.json` 改为由清单生成，
  面板展示的节点 IP/主机名/服务列表始终跟随 inventory
- **`cloudflared/config.yml`、`generate-keys.sh`** 改为引用清单值
  （库不可用时保留原值作回退默认），`generate-keys.sh` 的监听端口同步参数化

### 其他改进

- `lib-nodes.sh` 内置 `set -u` 与通用日志函数，`load_nodes` 空值行守卫
- `test_validate.py` 新增第 13 组测试「**IP/主机名可自定义性**」：
  校验功能脚本无硬编码 IP、每个节点 hostname 字段齐全、环境变量覆盖真实生效
- 验证项从 176 扩至 **184**（全部通过、0 警告）
- 新增 `inventory/nodes.local.yaml.example` 覆盖模板（真实覆盖文件已 gitignore）

---

## 🧹 v1.1.0 变更说明

### 清理

移除了一批非功能性的临时/开发辅助脚本与产物（GitHub 上传辅助、base64 中转、
临时文件读取脚本、重复验证脚本变体、面板重复副本 `app_github.py`、缓存目录等），
并恢复了被误删的 `.gitignore`。

### 修复的功能缺陷

| 问题 | 影响 | 修复 |
|------|------|------|
| `bootstrap.sh` 不解析命令行参数 | README 记载的 `--node/--ip` 被静默忽略，脚本仍走交互式提问，自动化场景必然失败 | 补齐参数解析，支持 `--node/--ip/--hostname/--sd/--yes`，无 TTY 时安全退出 |
| `deploy.sh` 缺少 `--exec` | README 记载的用法直接报「未知选项」 | 实现 `--exec`，分发后在节点服务目录远程执行命令 |
| `deploy.sh -n <节点名>` 参数解析错误 | 把节点名当成 `NAME\|IP\|ROLE` 记录解析，IP 为空导致连不上 | 改为按节点表查找，并兼容 `edge-01` 简写 |
| `wireguard-setup.sh` 无 `add peer` 子命令 | 运维手册 5.2 记载的用法完全不可用 | 实现 `add peer` / `list` / `gen`，含重名与 IP 冲突校验 |
| `wireguard-setup.sh` 输出目录错误 | 配置生成到仓库根的 `wireguard/`，`deploy.sh` 永远不会分发 | 改为写入 `node-<名称>/wireguard/wg0.conf`，与分发路径一致 |
| `generate-keys.sh` 重复 `PostUp`/`PostDown` | wg-quick 只保留最后一条，NAT/MASQUERADE 规则被静默丢弃 | 合并为单条 `PostUp`/`PostDown` |
| `restore.sh` 不支持简写形式 | 运维手册的 `restore.sh <ID> <服务名>` 会落到 usage 退出 | 支持自动识别节点/服务简写，并支持节点名归一化 |
| `update-all.sh` 传给 `apt upgrade` 的参数含表头 | `apt list` 的 `Listing... Done` 被当包名，更新直接报错 | 过滤表头，仅提取真实包名 |
| `install-services.sh` 在 Debian 12+ 安装失败 | PEP 668 (externally-managed) 导致 `pip3 install` 被拒 | 增加 `--break-system-packages` 与 apt 回退；`cd` 前加目录校验 |
| `services.yaml` 中 Home Assistant 同时声明 `ports` 与 `network_mode: host` | 两者互斥，docker compose 会拒绝 | 移除 `ports` |
| `setup.sh` 未选择服务时展开空数组 | `set -u` 下可能异常 | 增加空选择守卫 |
| WireGuard 私钥可能入库 | 生成的 `wg0.conf` 含私钥 | `.gitignore` 增加 `wireguard/` 与 `node-*/wireguard/wg0.conf`，并移除过期的占位配置 |

### 文档修正

- 修正 `operations.md` 中 `docker-compose pulldocker-compose up -d` 的拼接错误
- 修正 compose 工作目录（`/mnt/sd/<节点>` → `/mnt/sd/srv/<节点>`）
- 修正节点重建流程中的 `restore.sh latest.tar.gz`（应为备份 ID）
- 统一 `architecture.md` 中应用数据目录为 `wk-<节点>` 命名
- 新增本节与各脚本的完整用法说明

---

## 📝 License

MIT License
