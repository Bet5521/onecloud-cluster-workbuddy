# 🛠️ OneCloud 集群部署与运维指南

> 基于玩客云 WS1608 (Amlogic S805, ARMv7, 1GB RAM) 多节点组建的家庭服务集群

**当前版本: v1.4.1**

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
├── init/                     # 交互式初始化入口 (部署面板 / 部署节点 / 节点维护)
├── scripts/                  # 运维脚本
└── test_validate.py          # 集群配置/脚本验证套件
```

---

## 🚀 快速开始

> **第一次接触本项目？先跑交互式入口。** 不用记各种子命令与参数：
>
> ```bash
> bash init/init.sh
> ```
>
> 菜单方式引导完成「部署 Panel 面板 / 部署节点 / 节点维护 / 配置分发 / 服务安装 / 环境自检」，
> 全程逐项询问、不预置任何默认参数。详见 [init/README.md](init/README.md)。
>
> 需要脚本化或无人值守时，用下面各节的显式命令。

### 1. 初始化节点

在每个节点上执行一次（支持非交互参数，便于批量/自动化）：

```bash
./scripts/bootstrap.sh --node wk-edge-01    --ip 192.168.1.101 --hostname edge-01    --yes
./scripts/bootstrap.sh --node wk-iot-02     --ip 192.168.1.102 --hostname iot-02     --yes
./scripts/bootstrap.sh --node wk-storage-03 --ip 192.168.1.103 --hostname storage-03 --yes
```

> 不带参数运行时会进入交互式提问；非交互环境（cron / CI）请务必加 `--yes`。

#### IP 与网关的取值规则

| 项目 | 优先级 |
|---|---|
| IP | `--ip` > 本机探测（交互询问 / `--yes` 自动采用）> 清单 |
| 网关 | `--gateway` > 由最终 IP 推导（网络地址+1）> 本机探测 > 清单 |
| 前缀 | 本机探测 > 清单 `lan_subnet` > 24 |

关键行为：

- **脚本一启动就探测本机现状。** 在解析参数之前先读取本机当前 IP / 前缀 / 默认网关
  以及可移动存储，用于后续比对与风险提示。`--no-detect` 只是不自动采用这些值，
  探测本身仍会执行（否则无法做网段比对）。
- **换了网段，网关会跟着变。** 指定 `--ip 192.168.6.101` 后，脚本按同网段推导出
  `192.168.6.1`；与现网关不在同一网段时会提示并询问是否调整，`--yes` 下自动调整。
  显式给了 `--gateway` 则不推导（尊重明确意图），但若网段对不上仍会告警。
- **自动探测当前设备的网络。** 未指定 `--ip` 时先读取本机当前的 IP/前缀/默认网关，
  询问是否直接采用；`--yes` 下自动采用。不想采用加 `--no-detect`。
- 配置确认页会标注每个值的来源（命令行 / 本机探测 / 清单 / 由IP推导）。

#### SD 卡处理

启动时自动探测可移动存储（`lsblk` 的 removable/USB 通道，退化时按"非系统 eMMC"启发式判断）：

| 情况 | 行为 |
|---|---|
| 未插卡 | 打印提示后**跳过**挂载与 Docker 数据迁移，Docker 保留 `/var/lib/docker` |
| 检测到（或 `--sd` 指定） | 询问：是否挂载 → 挂载点（默认 `/mnt/sd`）→ 是否写入 fstab 开机自动挂载 |
| `--yes` | 按默认值自动执行（挂载 `/mnt/sd` + 写 fstab） |
| `--no-sd` | 完全跳过 SD 相关步骤 |
| `--sd-mount DIR` | 改用其它挂载点（会告警：集群脚本默认读写 `/mnt/sd`） |
| `--no-sd-automount` | 挂载但不写 fstab，重启后失效 |

> 挂载点不是 `/mnt/sd` 时，`deploy.sh` / `backup.sh` / `setup.sh` 等仍按 `/mnt/sd`
> 读写，需自行同步修改，否则数据会落到不同位置。

#### 执行前的网络安全检查

配静态 IP 是容易把机器"配失联"的操作，脚本在写入前会做三项检查并汇总告警：

1. **网段比对** —— 新 IP 与本机当前 IP 不在同一网段
2. **IP 冲突** —— 目标 IP 已被占用（ping 有响应）
3. **网关可达性** —— 网关当前 ping 不通

存在任一风险时：交互模式要求输入 `yes` 才继续（其余按键取消）；`--yes` 下仅告警后
继续；`--dry-run` 只提示不改动。

```bash
# 换网段部署: 网关自动算成 192.168.6.1
./scripts/bootstrap.sh --node wk-edge-01 --ip 192.168.6.101 --hostname edge-01 --yes

# 新节点: 直接用这台机器当前拿到的 IP 和网关
./scripts/bootstrap.sh --node wk-new-04 --hostname new-04 --yes

# 网关不是 .1 的网段: 显式指定, 不会被推导覆盖
./scripts/bootstrap.sh --node wk-edge-01 --ip 192.168.6.101 --gateway 192.168.6.254 --yes

# 先预览不落盘 (含 SD 挂载计划与网络风险提示)
./scripts/bootstrap.sh --node wk-edge-01 --ip 192.168.6.101 --dry-run

# 不插 SD 卡 / 挂载到别处且不自动挂载
./scripts/bootstrap.sh --node wk-edge-01 --ip 192.168.6.101 --no-sd --yes
./scripts/bootstrap.sh --node wk-edge-01 --ip 192.168.6.101 --sd-mount /mnt/data --no-sd-automount --yes

# 自动化场景可强制指定是否当作交互终端 (1=交互 0=非交互)
ONECLOUD_BOOTSTRAP_TTY=0 ./scripts/bootstrap.sh --node wk-edge-01 --ip 192.168.6.101 --yes
```

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
  `--ip/--hostname` 可再覆盖；网关、DNS、`/etc/hosts` 全部参数化。
  **IP 与网关联动**：`--ip` 换了网段时网关按同网段推导并询问确认（见「快速开始」）
- 所有运维脚本（deploy / update-all / health-check / backup / restore /
  wireguard-setup / setup / install-services）均从 `scripts/lib-nodes.sh`
  动态读取节点，**功能脚本中无任何硬编码 IP**
- **改完 IP 记得重新渲染面板配置与节点 `.env`**（两个生成脚本）：

  ```bash
  ./scripts/gen-panel-config.sh   # 面板展示的节点 IP
  ./scripts/gen-node-env.sh       # 各节点 .env 的 NODE_IP/WG_IP/DOMAIN
  ```

  `gen-node-env.sh` 只覆盖 `NODE_NAME/NODE_IP/WG_IP/DOMAIN` 四个托管键，
  **已填写的密钥等其它值原样保留**，可放心重复执行
- `backup.sh` / `restore.sh` 的备份目录可用 `BACKUP_DIR` 环境变量自定义
- 新增节点：写进 `nodes.local.yaml` 即被所有脚本识别，无需改代码

---

## 🔧 运维脚本

| 脚本 | 作用 | 常用用法 |
|------|------|---------|
| `lib-nodes.sh` | 节点清单库（单一数据源，供所有脚本 source） | 被其他脚本引用 |
| `gen-panel-config.sh` | 从清单生成 `panel/config.json` | 直接运行 |
| `gen-node-env.sh` | 从清单渲染各节点 `.env`（保留已填密钥） | `[节点名]` / `--dry-run` |
| `bootstrap.sh` | 新节点初始化（主机名/源/swap/SD卡/Docker/静态IP） | `--node <名> [--ip <IP>] [--gateway <IP>] [--sd <DEV>] [--no-sd] [--dry-run] --yes` |
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

项目自带验证套件，共 20 组，覆盖配置完整性、脚本语法、节点映射、服务一致性、
**文档化 CLI 接口契约**、**IP/主机名可自定义性**、**安全健壮性回归**、
**面板前后端契约**、**bootstrap 网络取值与 SD/风险预检**、**init 交互式入口契约**、
**Python 依赖降级链**（当前 259 项）：

```bash
python3 test_validate.py
```

输出示例：

```
  总计: 259 项
  通过: 259
  失败: 0
  警告: 0
```

报告同时写入 `test_report.txt`（已被 gitignore）。

---

## 📚 详细文档

| 文档 | 内容 |
|------|------|
| [init/README.md](init/README.md) | 交互式初始化入口（菜单结构、功能映射、面板部署细节） |
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

## 🚀 v1.4.1 变更说明

**修复：面板依赖安装在「python3 在、pip 不在」的机器上直接失败。**

在玩客云（Debian 12 / Armbian）上跑 `init.sh` 部署面板，会卡在这里：

```
是否安装/更新 Python 依赖 (panel/requirements.txt)? [y/n]: y
/usr/bin/python3: No module named pip
[WARN] 常规安装失败, 尝试 --break-system-packages (Debian 12+ / PEP 668)
/usr/bin/python3: No module named pip
[ERROR] Python 依赖安装失败
```

根因：装了 `python3` 但没装 `python3-pip`。此时 `--break-system-packages`
**完全无效** —— 它只是 pip 的**旗标**（用于绕过 PEP 668 的
`externally-managed-environment` 限制），**补不了缺失的 pip 自身**。

修复 —— 新增 `scripts/lib-pydeps.sh`，作为 Python 依赖安装的**单一实现**：

- **由轻到重四路降级**，任一路成功**且 `import` 校验通过**才算成功：
  1. `pip install`
  2. `pip install --break-system-packages`（PEP 668）
  3. pip 缺失 → `ensurepip` → `apt install python3-pip` → `get-pip.py` 补 pip，再回 1/2
  4. pip 彻底不可用 → `apt install python3-flask python3-flask-cors`，**完全绕开 pip**
- **不轻信退出码**：每路之后实跑一次 `import flask, flask_cors`，
  pip 谎报成功也会继续降级
- **依赖装进系统解释器**（不用 venv）—— 面板 systemd 单元执行的是 `/usr/bin/python3`
- **失败时打印可复制的兜底命令**（pip 与 apt 两条）
- 同一实现被 `init/init.sh`、`scripts/setup.sh`、`scripts/install-services.sh` 复用。
  这三处原先各自拼 `pip3 install`（`setup.sh` 还带 `|| true` 静默吞错），现已统一
- `init.sh` 中两处系统改动（装 `python3-pip`、装发行版包）**都会先询问**，
  以符合「纯交互、不预设任何默认」的约定

验证：新增第 20 组测试 15 项，用 mock 解释器 + mock `apt-get` 实测 8 个场景
（依赖已齐 / 常规 pip / PEP 668 / ensurepip 补齐 / apt 补齐 pip /
apt 装发行版包 / 全部失败 / pip 谎报成功）。

---

## 🚀 v1.4.0 变更说明

本轮新增 `init/` —— 一个**纯交互式**的初始化与运维总入口，把原先散落在
`scripts/` 与 `panel/` 的操作收进一个菜单；同时加固了面板服务的参数注入与仓库行尾一致性。

### 新增 init/ 交互式入口

```bash
bash init/init.sh
```

| 菜单 | 能力 |
|------|------|
| 1) 部署 Panel 控制面板 | systemd 常驻 / 前台试运行 / 仅装依赖 |
| 2) 部署节点 | 本机 bootstrap（不带参数，由 bootstrap 自己提问）/ SSH 接力打开远端入口 / 查看节点清单 |
| 3) 节点维护 | 健康巡检 / 备份 / 恢复 / 批量更新 |
| 4) 配置与分发 | deploy / 测连通 / 预览 / 远程执行 / 面板配置 / 节点 .env / WireGuard |
| 5) 服务安装 | setup.sh 统一安装 / 原生服务 / 节点容器 |
| 6) 环境自检 | 目录、脚本、依赖命令、本机网络、节点清单逐项体检 |

三条硬约束：

| 约束 | 实现 |
|------|------|
| 不接受任何命令行参数 | `[ "$#" -gt 0 ]` 直接 `exit 2` 并提示直接运行 |
| 不预置任何默认参数 | 脚本内不出现 `--yes`；取值项（端口 / 账号 / 密码 / 备份目录）**全部必填** |
| 无隐式默认值 | 不用 `[Y/n]` 式回车默认，确认必须显式敲 `y` / `n` |

设计上**只做编排、不重复实现**：面板 systemd 安装仍复用 `panel/install-service.sh`，
自定义端口/账号经 `/etc/onecloud/panel.env`（权限 600）+ systemd drop-in 注入；
本机 bootstrap 不附加任何参数；远程节点用 `ssh -t` 打开远端的同一个入口。

> 因为没有默认参数，本入口**不服务自动化/CI**。批量与无人值守请直接调 `scripts/` 下脚本并显式传参。

### 加固

| 问题 | 影响 | 修复 |
|------|------|------|
| `panel/install-service.sh` 把 `PANEL_PORT=9000` / `PANEL_HOST=0.0.0.0` 写死在 unit 里 | 自定义端口只能靠 drop-in 里 `EnvironmentFile` 的覆盖顺序生效，属于隐性依赖 | unit 改由 `PANEL_HOST` / `PANEL_PORT` 环境变量渲染（保留 9000 / 0.0.0.0 默认值）；`init.sh` 用 `sudo env PANEL_HOST=… PANEL_PORT=…` 显式注入（规避 `sudo` 的 `env_reset`），与 `EnvironmentFile` 形成双保险 |
| 仓库无 `.gitattributes`，Windows 工作区被 checkout 成 CRLF | `./scripts/xxx.sh` 报 `bad interpreter: /bin/bash^M`；bash 变量尾部混入 `\r` 导致字符串比较莫名失败 | 新增 `.gitattributes` 统一 `* text=auto eol=lf`，二进制与将来可能的 Windows 脚本单独声明；工作区现存 CRLF 一并归一化为 LF |
| `wireguard-setup.sh list` 中文表头按字节填充 | `printf '%-16s'` 按字节而非显示宽度填充，表头比数据列宽 2 格，表格错位 | 表头改为按显示宽度手工排版（与 `init.sh` 的处理一致） |

### 文档与验证

- 新增 `init/README.md`：菜单结构、功能到脚本的映射、面板部署细节、前置条件、常见问题
- 根 README 登记 `init/` 入口，并补充本节变更说明
- `test_validate.py` 218 → **243 项**，全量通过、0 失败 0 警告：
  - 第 18 组「init 交互式入口」14 项：拒绝参数、无 `--yes`、无回车默认、交互函数与菜单齐全、
    引用的 10 个 `scripts/*.sh` 全部存在、面板安装复用、零硬编码 IP、文档登记
  - 第 19 组「交付物一致性」11 项：版本声明四处一致、`.gitattributes` 锁定 LF、
    工作区脚本无 CRLF、面板监听参数可注入且 unit 用注入值、无中文表头按字节填充
- 交互实测：`init.sh` 全部菜单分支 43 项探针通过（含各"取消"路径、端口校验、`pick_node`
  越界与非法名、非法菜单输入恢复）
- 面板端到端实测：真实启动 `app.py` 后验证 — 首页与静态资源、未认证/错误密码 401、
  白名单拒绝 `rm -rf` 与 `free; cat /etc/shadow`、放行 `ls` 与 `ls -la`、
  未知节点 404 / 未知动作 400 / 危险操作二次确认 400、节点离线时 `/api/status` 正常降级
- `bootstrap.sh --dry-run` 实测：改 IP 后网关自动由同网段推导（`192.168.6.101` → `192.168.6.1`），
  无 SD 卡时跳过挂载与 Docker 数据迁移
- 生成类脚本实测：`gen-node-env.sh --dry-run`、`gen-panel-config.sh`（幂等，重复执行内容不变）、
  `wireguard-setup.sh list`

---

## 🚀 v1.3.0 变更说明

本轮把 `bootstrap.sh` 从「按清单写死网络参数」改造为「以参数与本机实际网络为准」，
并修复了面板的若干契约缺陷，同时补齐各目录 README。

### bootstrap.sh：参数优先 + 本机探测

| 能力 | 说明 |
|------|------|
| **启动即探测** | 解析参数**之前**先读取本机当前 IP / 前缀 / 默认网关与可移动存储，供后续网段比对与风险提示使用 |
| **IP 与网关联动** | `--ip 192.168.6.101` 会按同网段推导出网关 `192.168.6.1`；与现网关不同网段时提示并询问是否调整，`--yes` 下自动调整 |
| **SD 卡按需挂载** | 自动识别可移动存储：未插卡 → 跳过挂载**且跳过 Docker 数据迁移**（保留 `/var/lib/docker`）；有卡 → 依次询问 是否挂载 / 挂载点（默认 `/mnt/sd`）/ 是否写入 fstab |
| **联网风险预检** | 写入静态 IP 前检查三项：跨网段 / 目标 IP 已被占用 / 网关不可达。命中任一，交互模式要求输入 `yes` 确认，`--yes` 下仅告警 |
| **交互判定可注入** | `ONECLOUD_BOOTSTRAP_TTY=1/0` 可强制指定是否当作交互终端，便于自动化与测试 |

新增参数：

| 参数 | 作用 |
|------|------|
| `--gateway <IP>` | 显式指定网关，不参与推导（网段对不上会告警） |
| `--sd <DEV>` / `--sd-mount <DIR>` | 指定 SD 卡设备 / 挂载点 |
| `--no-sd` / `--no-sd-automount` | 不挂载 SD 卡 / 不写入 fstab |
| `--no-detect` | 不自动采用本机探测到的 IP 与网关（探测照跑，仅影响是否采用） |
| `--dry-run` | 只预览将做什么，不改动系统 |

取值优先级：

| 项目 | 优先级 |
|------|--------|
| IP | `--ip` > 本机探测（询问 / `--yes` 自动采用）> 清单 |
| 网关 | `--gateway` > 由最终 IP 推导（网络地址 +1）> 本机探测 > 清单 |
| 前缀 | 本机探测 > 清单 `lan_subnet` > 24 |

### 面板修复

| 问题 | 影响 | 修复 |
|------|------|------|
| 集群「全部启动/停止/拉取镜像」只弹提示、不调 API | 按钮点了**毫无效果** | 遍历各节点真实下发请求并汇总结果，`docker_down` 加二次确认 |
| 服务「日志」按钮只提示成功 | 看不到任何日志内容 | 输出改写到命令输出区 |
| 命令白名单写成 `"ls "`（带尾空格） | 单独执行 `ls` 被误拒，与文档不符 | 修正为 `ls` |
| 命令白名单仅做前缀匹配 | `free; cat /etc/shadow` 以 `free` 开头被放行 | 拦截 `;` `&&` `\|\|` `\|` 反引号 `$(` 等元字符 |
| 服务操作 HTTP 方法不匹配、健康检查与脚本参数处理有误 | 相关按钮与检查失效 | 按后端实际契约修正 |

### 文档

- 新增 `scripts/`、`panel/`、`inventory/`、`docs/` 及三个节点目录的 README（共 7 份），
  其中路径与命令均逐一实测，消除「文档写了但跑不通」

### 验证

- `test_validate.py` 194 → **218 项**（新增第 15 组「面板前后端契约」、
  第 16 组「bootstrap 网络取值」、第 17 组「bootstrap SD 与风险」），全部通过、0 警告
- bootstrap 相关测试用 mock 出的 `ip`/`lsblk`/`findmnt`/`ping` 驱动，
  探针截取到「第 2 步之前」，不触碰真实系统
- `--help` 改为短路，不再触发本机探测

---

## 🔍 v1.2.1 变更说明

对 v1.2.0 做了一轮完整回归验证，修复验证中发现的问题：

### 修复

| 问题 | 影响 | 修复 |
|------|------|------|
| `restore.sh latest` 在无任何备份时，`BACKUP_ID` 解析为空 | `BACKUP_PATH` 退化成备份根目录并通过存在性检查，会把**整个备份目录**当作一次备份 rsync 出去 | 加空值守卫，直接报错退出 |
| `install_mihomo` 架构写死 `armv7` | 非 armv7 设备会装错二进制 | 按 `uname -m` 自动识别 armv7/arm64/amd64 |
| 二进制下载不校验产物 | 网络失败或 404 时，`gunzip`/`tar` 仍会写出空文件或错误页面，`chmod +x` 后照常打印「安装成功」 | 下载后校验 ELF 魔数，非可执行文件即中止 |
| 原生安装缺少 root 检查 | 非 root 运行时报「No such file or directory」，原因不明 | 增加 `require_root`，给出明确提示 |
| `all-native` 无容错 | `set -e` 下任一原生服务失败会中断后续安装 | 与 `all-docker` 一致，改为 `|| log_warn` |
| 节点内 `clash/install-binary.sh`、`xiaomusic/install.sh` 重复实现安装逻辑 | 与 `install-services.sh` 行为分叉，且仍带着上面几个缺陷 | 改为委派统一脚本的兼容入口（保留原路径与目录初始化） |
| 面板命令白名单只做前缀匹配 | `free; cat /etc/shadow` 会以 `free` 开头被放行，黑名单拦不住 | 拦截 `;` `&&` `\|\|` `\|` 反引号 `$(` 等元字符 |
| 自定义 IP 无法贯通到容器运行时 | 节点 `.env` 是静态模板，改了清单后容器内 `NODE_IP` 仍是旧值 | 新增 `scripts/gen-node-env.sh` 从清单渲染 `.env` |

### 验证

- `test_validate.py` 184 → **194 项**（新增第 14 组「安全与健壮性回归」，
  把本轮修复全部固化为回归用例）
- 27 个 shell 脚本 `bash -n` 通过；JSON/YAML 全过
- 文档中所有脚本路径与参数形式逐一实测（backup/restore/install-services/
  update-all/wireguard 各分支），无「文档写了但跑不通」的项

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
