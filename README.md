# 🛠️ OneCloud 集群部署与运维指南

> 基于玩客云 WS1608 (Amlogic S805, ARMv7, 1GB RAM) 多节点组建的家庭服务集群

**当前版本: v1.5.4**

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
│   ├── cloudflare-setup.md   # Cloudflare Tunnel 配置
│   └── firewall/             # 防火墙建议清单 (每节点一份, deploy 后自动生成)
│       └── <节点名>.txt      # 可直接录入 setup_firewall.sh 的规则行
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

#### 初始化装包范围（无头服务器）

目标机是玩客云：**无图形界面、1GB 内存、eMMC/SD 卡**。装包按三档处理，
只装"部署链路真的会调用"的包：

| 档位 | 包 | 默认 |
|---|---|---|
| **核心** | `curl` `git` `ca-certificates` `jq` `rsync` `parted` `wireguard-tools` | ✅ 装 |
| **可选** | `wget` `vim` `htop` `iotop` `net-tools` `dnsutils` `unzip` `dosfstools` `fdisk` `lsb-release` `gnupg` | ❌ 不装，`--extra-pkgs` 开启 |
| **桌面/图形** | 桌面套件 / Xorg / 显示管理器 / 字体 / 浏览器 / 远程桌面等 | ⛔ 永不装 |

- 核心包逐项对应一条真实依赖：下载（`curl`+`ca-certificates`）、克隆仓库（`git`）、
  解析 JSON（`jq`）、迁移 Docker 数据（`rsync`）、SD 卡分区（`parted`）、组网（`wireguard-tools`）。
- `iproute2` / `e2fsprogs` / `util-linux` 属系统基础包，系统一定自带，不重复声明。
- 可选包**没有被删除，只是移出默认流程**：`--extra-pkgs` 装预设、`--extra-pkgs "vim htop"` 装指定的。
- 桌面/图形包即使被显式列出也会被剔除并告警（黑名单见 `bootstrap.sh` 的 `APT_GUI_DENY`）。

```bash
# 只装核心包 (默认)
./scripts/bootstrap.sh --node wk-edge-01 --yes
# 另外装可选工具 (预设清单)
./scripts/bootstrap.sh --node wk-edge-01 --yes --extra-pkgs
# 只装指定的可选工具
./scripts/bootstrap.sh --node wk-edge-01 --yes --extra-pkgs "vim htop"
ONECLOUD_EXTRA_PKGS="vim htop" ./scripts/bootstrap.sh --node wk-edge-01 --yes   # 等价写法
```

`setup.sh` 同样只补业务必需的命令（`jq` / `curl` / `parted`），不再为了端口检测去装 `net-tools`、
不再默认装 `dosfstools`（格式化一律走 ext4）。

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

# apt 动作默认全部跳过; 需要时显式开启 (换源会自动补一步 apt update, 但不升级系统包)
./scripts/bootstrap.sh --node wk-edge-01 --ip 192.168.6.101 --mirror --yes
./scripts/bootstrap.sh --node wk-edge-01 --ip 192.168.6.101 --mirror --apt-upgrade --yes
# 完全不动 apt (纯离线初始化: 只配主机名/IP/存储/目录/SSH 密钥)
./scripts/bootstrap.sh --node wk-edge-01 --ip 192.168.6.101 --no-apt --yes
# 可选工具 (vim/htop/iotop/...) 默认不装, 需要时显式开启
./scripts/bootstrap.sh --node wk-edge-01 --ip 192.168.6.101 --extra-pkgs --yes
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
PANEL_USER=admin PANEL_PASS='强密码' PANEL_PORT=9000 PANEL_HOST=192.168.1.101 python3 app.py
```

**监听地址（`PANEL_HOST`）怎么选**：想「同网段可访问」应填**本机在该网段的地址**
（如 `192.168.1.101`），而不是 `0.0.0.0` —— 后者会在**所有**网卡上监听
（含 WireGuard 与外网网卡），暴露面更大。

| 取值 | 谁能访问 |
|------|---------|
| `0.0.0.0` | 所有网卡上的所有来源（需配合防火墙） |
| `192.168.1.101` | 同网段可直接访问，其它网段需经路由/防火墙 |
| `127.0.0.1` | 仅本机（远端需 `ssh -L 9000:127.0.0.1:9000 <用户>@<节点IP>`） |

填错的代价不低：网段地址（`192.168.1.0`）、广播地址（`192.168.1.255`）、
回环网段的网络地址（`127.0.0.0`）都不会当场报错，要等 systemd 拉起面板才以
`Cannot assign requested address` 失败。所以 `PANEL_HOST` 会在**三处**被校验
（`init.sh` 交互引导 / `install-service.sh` 入参 / `app.py` 启动前），共用
`scripts/lib-panel-host.sh` 一套判定，并给出可直接采用的替代值——
例如 `192.168.1.0` 会提示改用本机在该网段的 `192.168.1.101`。

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
网络参数用 `ONECLOUD_GATEWAY` / `ONECLOUD_DNS` / `ONECLOUD_DOMAIN` / `ONECLOUD_WG_PORT` / `ONECLOUD_LAN_PREFIX` / `ONECLOUD_WG_SUBNET`；
其中 **DNS 默认为 `dhcp`（自动获取）** —— 脚本不向系统写入任何 nameserver，交给 DHCP / 网络管理器 /
系统现状决定；需要写死时把 `network.dns`（或 `--dns` / `ONECLOUD_DNS`）填成具体地址，多个用逗号分隔，
`bootstrap.sh` 交互式运行直接回车也等于"自动获取"；
`bootstrap.sh` 的 apt 动作**默认全部跳过**（v1.4.5 起）：换源用 `--mirror`
（或 `ONECLOUD_APT_ENABLE_MIRROR=1`）、刷新索引用 `--apt-update`、升级系统包用
`--apt-upgrade`、跳过装基础工具用 `--no-apt-pkgs`、四项一起跳过用 `--no-apt`；
镜像可用 `ONECLOUD_APT_MIRROR` / `ONECLOUD_APT_SECURITY_MIRROR` 自定义，
`ONECLOUD_DEBIAN_CODENAME` 强制指定代号，旧开关 `ONECLOUD_APT_SKIP_MIRROR=1` 仍然有效。

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
  **DNS 默认自动获取（DHCP）**：`--dns dhcp`（或 `--dns auto`/`--dns-dhcp`）、清单 `dns: dhcp`、
  交互式直接回车都等于该模式，脚本不会写死 `dns-nameservers`（netplan 下写成 `dhcp4: true`
  且 `use-routes: false`，只从 DHCP 取 DNS 不抢默认路由）；要固定解析就传 `--dns 223.5.5.5,1.1.1.1`。
  注意脚本配的是**静态 IP**，若该机已无 DHCP 客户端在跑，自动获取会拿不到 DNS —— 碰到解析异常
  重新执行并指定 `--dns <地址>` 即可。
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
| `lib-network-audit.sh` | 通路/防火墙/SSH 通道自检库（只读探测） | 被 `bootstrap.sh` 引用 |
| `gen-panel-config.sh` | 从清单生成 `panel/config.json` | 直接运行 |
| `gen-node-env.sh` | 从清单渲染各节点 `.env`（保留已填密钥） | `[节点名]` / `--dry-run` |
| `bootstrap.sh` | 新节点初始化（主机名/swap/SD卡/Docker/静态IP；apt 换源与更新默认跳过） | `--node <名> [--ip <IP>] [--gateway <IP>] [--sd <DEV>] [--no-sd] [--mirror] [--apt-update] [--no-apt] [--dry-run] --yes` |
| `setup.sh` | 统一安装（端口检测 + 多选批量安装 + 磁盘挂载） | `sudo bash setup.sh` |
| `wireguard-setup.sh` | WireGuard mesh 配置生成（**默认不写防火墙规则**） | `gen` / `add peer` / `list`；`--with-wg-firewall` 可恢复自带规则 |
| `firewall-recommend.sh` | 生成防火墙建议清单（静态推算，不改任何防火墙） | 直接运行 / `[节点名]` / `--emit-dsl` / `--out DIR` / `--lan <网段>` |
| `deploy.sh` | rsync 分发配置到各节点 | `-n <节点>` / `--exec <命令>` / `-t` / `-d` |
| `install-services.sh` | 安装原生二进制或启动节点容器 | `mihomo` / `edge` / `all-native` |
| `health-check.sh` | 集群健康巡检（SSH/容器/端口/负载/OOM） | 直接运行 |
| `backup.sh` | 备份配置与数据（`BACKUP_DIR` 可自定义） | `all` / `config` / `data` / `node <名>` / `service <名>` |
| `restore.sh` | 从备份恢复 | `<备份ID> <目标>` 或 `<备份ID> service <名>` |
| `update-all.sh` | 批量更新镜像/系统包（不升 Docker Engine） | `-d` / `-s` / `-a` / `-n <节点>` |

所有脚本均支持 `-h` / `--help` 查看完整用法。

---

## ✅ 功能验证

项目自带验证套件，共 28 组，覆盖配置完整性、脚本语法、节点映射、**服务清单五方一致性**、
**文档化 CLI 接口契约**、**IP/主机名可自定义性**、**安全健壮性回归**、
**面板前后端契约**、**bootstrap 网络取值与 SD/风险预检**、**init 交互式入口契约**、
**Python 依赖降级链**、**bootstrap apt 源与依赖安装回归**、
**bootstrap DNS 模式（DHCP 自动获取）**、**面板监听地址校验（误填拦截 / 同网段引导）**、
**通路 / 防火墙 / SSH 通道自检**、**面板安装参数（监听地址与访问地址分离）**、
**部署侧零防火墙改动 + 防火墙建议清单生成**、
**脚本 usage 与实现一致性（声明的子命令必须有 case 分支）**
（当前 441 项）：

```bash
python3 test_validate.py
```

输出示例：

```
  总计: 441 项
  通过: 441
  失败: 0
  警告: 0
```

> 在 Windows / Git Bash 上跑一轮约 15–20 分钟（被测脚本每调一次外部命令都是一次
> 进程创建）。最重的几个 bash harness 默认给 900 秒上限，机器慢或同时跑别的任务时
> 可以调大：`ONECLOUD_TEST_HARNESS_TIMEOUT=1800 python3 test_validate.py`。

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
| [docs/package-trim.md](docs/package-trim.md) | 初始化装包精简说明（核心/可选/桌面图形三档与移除理由） |
| [docs/to-fix.md](docs/to-fix.md) | 全项目验证问题清单与修复记录 |
| [docs/firewall/](docs/firewall/) | 各节点防火墙建议清单 |

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

## 🚀 v1.5.4 变更说明

**主题：SD 卡工具箱 —— 格式化 / 迁移 / 更换备份，闭环 SD 卡全生命周期管理。**

### 背景

v1.5.3 已实现「安装路径自适应」：无 SD 卡时回退 eMMC 的 `/opt/onecloud`，有 SD 卡时落到 SD。
但还缺少把「eMMC 上的存量 → SD」「SD 内容 → 换卡备份」打通的运维手段。本版本补上工具箱。

### 新增脚本

- **`scripts/sd-format.sh`**：将 SD 卡整卡分区并格式化为单一 ext4 分区。仅对「可移动 SD 卡」操作，
  绝不格式化根磁盘；已是 ext4 默认跳过（`--force-fmt` 强制）；支持 `--dry-run` / `--yes`。
  迁移 / 更换流程检测到「SD 卡非 ext4」时会**自动触发**它。
- **`scripts/sd-migrate.sh`**：把无 SD 卡期间装在 eMMC 回退目录（`/opt/onecloud`）的组件 / 配置 /
  依赖文件，经 `rsync -aHAX` 完整迁移到已挂载 SD 卡；若 Docker 数据仍在 eMMC，则一并迁移到
  `<SD>/docker` 并改写 `/etc/docker/daemon.json` 的 `data-root`。默认保留来源（`--clean` 才删），
  `--dry-run` 只读预演、不破坏数据。
- **`scripts/sd-replace.sh`**：更换 SD 卡前，把 SD 卡全部内容 `tar -czf` 打包并转存到已插入的
  USB 设备（归档名 `onecloud-sd-backup-<主机>-<时间>.tar.gz`，附 SHA256 校验和）。执行前**先校验**
  USB 已挂载且剩余空间 ≥ SD 数据量 + 预留（默认 512MB），不满足直接中止；绝不把根盘 / SD 自身当 USB。
- **`scripts/sd-tools.sh`**：交互式入口（菜单 1 迁移 / 2 更换 / 3 格式化 / 0 退出）；
  也支持非交互透传 `migrate|replace|format [参数]`。

### 共同设计原则

- 设备 / 分区 / 挂载点一律**运行时探测**（`lib-install-path.sh` 的 `sd_probe` / `findmnt` / `lsblk`），
  不写死 `/dev/mmcblk1` 或 `/mnt/sd`。
- 复用 `ensure_sd_ready`（探测 → 必要时格式化 ext4 → 挂载 → 评估），与安装路径决策引擎一致。
- 全部支持 `--dry-run` 预演。

### 前置条件与注意事项

- 前置依赖：`rsync`、`parted`/`sfdisk` + `mkfs.ext4`（e2fsprogs）、`tar`；无头服务器核心装包档已含 `rsync` 与 `parted`。
- 格式化会清空 SD 卡数据，确认前务必确认无需备份。
- 迁移建议在**停止相关容器 / 服务**后进行，避免写入导致不一致。
- 更换备份须先插入空间足够的 USB 设备；大容量 SD 打包可能耗时，可先用 `--dry-run` 预估。
- 详细功能 / 前置 / 用法 / 注意事项见 [docs/sd-tools.md](docs/sd-tools.md)。

### 验证

- 新增第 **31** 组测试「**SD 卡工具箱（格式化/迁移/更换）**」**24 项**：脚本存在 + 语法 8 + 行为 16
  （格式化 4 / 迁移 5 / 更换 3 / 调度 3），覆盖自动格式化、根盘拦截、无 SD / 无 USB 拒绝、空间不足拒绝、
  备份校验与菜单透传。
- 全量 **496 项 / 31 组**，全部通过。

---

## 🚀 v1.5.3 变更说明

**主题：安装路径自适应 —— SD 卡可用时用 SD，否则（无卡/未挂载/只读/空间不足）自动回退 /opt，且不阻断初始化。**

### 决策流程

初始化阶段（`bootstrap.sh` 第 14 步）与安装阶段（`setup.sh` 全局配置）统一调用 `lib-install-path.sh` 的
`resolve_data_root()`，按 **设备存在 → 分区可见 → 已挂载（运行时查询）→ 挂载点可读写 → 剩余空间充足**
逐级判定：

- 全部满足 → 安装到 SD 卡**实际挂载点**（通过 `findmnt --source <设备>` 查询得到，不写死 `/mnt/sd`）
- 任意一级不满足 → 回退安装根目录到 `/opt/onecloud`，流程不中断

### 关键性质

- **SD 卡不是硬性前置**：无卡 / 未挂载 / 挂载只读 / 空间不足都是正常状态，只降级不报错
- **挂载路径运行时查询**：移除原 `bootstrap.sh` 写死的 `DATA_ROOT="/mnt/sd"` 与 `setup.sh` 的
  `[ -d /mnt/sd ]` 判定
- **写入过程异常回退**：`safe_install_dir` / `safe_install_file` 往 SD 写目录或文件失败时，自动把
  `DATA_ROOT` 切回 `/opt/onecloud` 重试，并输出 `[ERROR]`/`[WARN]` 明确状态
- 回退根目录、最小可用空间（`SD_MIN_SPACE_MB`，默认 512）均为可覆盖环境变量

### 实现与验证

- 新增 `scripts/lib-install-path.sh`：`sd_probe` / `sd_mount_state` / `sd_rw_ok` / `sd_space_ok` /
  `sd_evaluate` / `resolve_data_root` / `install_path_for` / `safe_install_dir` / `safe_install_tree` /
  `safe_install_file`
- 详细设计见 [docs/install-path.md](docs/install-path.md)
- 新增第 30 组测试「**安装路径自适应**」12 项：SD 可用 / 无设备 / 未挂载 / 写入失败降级 /
  空间不足 / 组件路径映射 / 静态接入点检查
- 全量 **472 项 / 30 组**，全部通过

---

## 🚀 v1.5.2 变更说明

**主题：初始化装包精简 —— 面向无头服务器，只装部署链路真正需要的包。**

### 装包分档（核心 / 可选 / 永不装）

`bootstrap.sh` 原来一次性装 18 个包，其中近一半与"无图形界面 + 部署流水线"的实际需要无关。
现在改为三档：

| 档位 | 内容 | 行为 |
|---|---|---|
| 核心（`BASE_PKGS`） | `curl` `git` `ca-certificates` `jq` `rsync` `parted` `wireguard-tools` | 默认装 |
| 可选（`OPT_PKGS_PRESET`） | `wget` `vim` `htop` `iotop` `net-tools` `dnsutils` `unzip` `dosfstools` `fdisk` `lsb-release` `gnupg` | 默认**不装**，`--extra-pkgs` 显式开启 |
| 桌面/图形（`APT_GUI_DENY`） | 桌面套件 / Xorg / 显示管理器 / 字体 / 浏览器 / 远程桌面 / 音频蓝牙等 45 项 | **永不装**，显式列出也会被剔除并告警 |

- 新增 `--extra-pkgs` / `--no-extra-pkgs` 与 `ONECLOUD_EXTRA_PKGS`：
  不带值装预设清单，带值只装指定的（`--extra-pkgs "vim htop"`）
- 新增 `pkg_gui_name()` / `pkg_gui_filter()` / `extra_pkgs_apply()`，
  黑名单按全名 + 命名前缀（`xserver-*` / `x11-*` / `fonts-*` / `*-desktop` …）双路匹配
- 本次**没有**发现脚本里原本存在桌面/图形包；加黑名单是为了防止后续被误加回来

### 一并收敛的 `setup.sh`

- `ensure_tools()` 只补 `jq` / `curl` / `parted`，不再默认装 `wget` 与 `dosfstools`
  （格式化一律走 `mkfs.ext4`，挂载 vfat/exfat 由内核 + `mount` 负责）
- `check_port()` 不再为了端口检测去装 `net-tools`（`ss` 来自 iproute2，必定存在），
  两者都缺失时只告警
- 删除已无引用的 `ensure_pkg()`

### 验证

- 新增第 29 组测试「**初始化装包精简**」19 项：核心清单精确匹配、11 个可选包未泄漏进默认流程、
  黑名单不误伤、`--extra-pkgs` 两种用法、显式列出图形包被剔除且有告警
- 全量 **460 项 / 29 组**，全部通过

---

## 🚀 v1.5.1 变更说明

**主题：补齐两条部署路径的能力差，并把「声明 vs 实现」的契约纳入回归测试。**

### 修复

- **`setup.sh` 补齐 3 个安装入口**：新增 `install_typecho` / `install_gitea` /
  `install_ariang`（镜像、端口、数据卷与 `docker-compose.yml` 对齐）。
  此前这三个服务只在 compose 路径能装，走「统一安装」菜单装不上，巡检随后报容器未运行。
- **`restore.sh` 服务白名单改为读 inventory**：原来硬编码 6 个服务，其余 12 个
  「备份得到、恢复不了」；现在与 `backup.sh` 同一口径（`service_names`），
  节点候选也改用 `node_resolve`，不再硬编码节点名。
- **`backup.sh` 补上 usage 承诺的 `data` 类型**（只备份应用数据，排除 compose 与 `.env`）；
  同时把类型校验提到 `mkdir` 之前，非法类型不再留下空备份目录。
- **面板页脚版本号改为后端注入**（原来硬编码 `v1.0`）。
- `deploy.sh` 远程预建目录补 `memos/data`、`typecho/usr`、`cups-web/config`。
- 删除未被前端调用的 `/api/topology` 死端点；清理顶层空目录 `wireguard/`
  与 `.gitignore` 里被整目录规则覆盖的重复条目。

### 验证增强

- **第 11 组升级为「服务清单五方比对」**：`nodes.yaml` / `services.yaml` /
  `docker-compose.yml` / `panel/config.json` / `setup.sh add_service` 五个数据源
  互相比对，任一处漂移直接 `log_fail`（原来只比两处且只 `log_warn`，
  所以上面的安装入口缺口能长期存在而测试全绿）。
- **新增第 28 组「脚本 usage 与实现一致性」**：usage 声明的子命令必须在 `case`
  里有分支（双向比对）、backup/restore 服务口径一致、非法参数不产生副作用、
  文档里写出来的命令必须真实存在。

---

## 🚀 v1.5.0 变更说明

**主题：部署脚本彻底不碰防火墙，防火墙改由一份"建议清单"统一交给你执行。**

### 1. 部署脚本不再写任何防火墙规则

`wireguard-setup.sh`（控制端生成）与 `node-wk-edge-01/wireguard/generate-keys.sh`
（节点本地生成）**默认不再往 `wg0.conf` 里写 `PostUp`/`PostDown`**。

原因：`wg-quick` 的 PostUp 会在节点上直接 `iptables -A`，而 onecloud 的定位是
"只部署、不碰系统安全策略"。谁在什么时候改了防火墙，必须只有一个入口。

- 需要恢复自带规则时：`ONECLOUD_WG_FIREWALL=1 ./scripts/wireguard-setup.sh gen`
  或 `--with-wg-firewall`；反向开关 `--no-wg-firewall`
- 不写规则时，脚本会在 `wg0.conf` 里留注释，并打印 Hub 节点需要手工执行的那几条命令

### 2. 新增防火墙建议清单生成器

```bash
./scripts/firewall-recommend.sh              # 全部节点 -> docs/firewall/<节点>.txt
./scripts/firewall-recommend.sh wk-edge-01   # 只出某个节点
./scripts/firewall-recommend.sh --emit-dsl   # 只打印可录入的规则行
./scripts/firewall-recommend.sh --stdout     # 打印完整报告不落盘
./scripts/firewall-recommend.sh --lan 192.168.1.0/24
```

清单**只做静态推算**（读 `inventory/nodes.yaml` + `inventory/services.yaml`），
不发任何网络请求、不碰节点：

- 默认策略建议（INPUT DROP / OUTPUT ACCEPT / FORWARD ACCEPT）
- 必需规则（SSH、面板、WireGuard）与内网规则（AdGuard 53、Grafana 3000…）分开列
- 只给**放行**建议，不替你决定封禁谁
- 容器变量端口（如 `${MEMOS_PORT}`）单独列出，标明"需人工确认"，不静默丢弃
- Hub 节点额外给一段 WireGuard 转发/NAT 命令（`sysctl`、`FORWARD`、`MASQUERADE`）
  —— 这部分 DSL 表达不了，必须手工录入

### 3. 部署流程接入

- `scripts/deploy.sh` 分发完成后自动生成清单（`--dry-run` / `-t` 时跳过；
  生成失败只告警，不影响节点分发）
- `init/init.sh` 维护菜单新增「生成防火墙设置建议清单」

### 4. 唯一执行入口

真正改防火墙**只有**一条路：把清单带到节点上，逐条录进 `setup_firewall.sh`
（`/etc/fw-setup/rules.dsl`）后由你手动应用。本仓库不含该脚本，也不调用它。

---

## 🚀 v1.4.5 变更说明

三件事：**换源/更新不再强制执行**、**通路自检（防火墙与 SSH 通道）**、
**面板安装可直接设置 IP / 端口 / 监听端口**。

### 1. apt 动作全部改为可选，默认跳过

此前初始化**必然换源 + 必然 `apt update/upgrade`**。现场代价不小：换源失败会留下
半截源文件；升级可能拉入新内核/firmware 让机器起不来；而多数节点跑脚本前源和
索引其实已经就绪，再动一遍纯属引入变量。现在四项各自独立，默认都不做：

| 动作 | 默认 | 开启方式 |
|------|------|---------|
| 换源 | **跳过** | `--mirror` / `ONECLOUD_APT_ENABLE_MIRROR=1` |
| 刷新索引 (`apt update`) | **跳过** | `--apt-update` |
| 升级系统包 (`apt upgrade -y`) | **跳过** | `--apt-upgrade` |
| 安装基础工具 | 执行 | 跳过用 `--no-apt-pkgs` |

- `--no-apt` 一键关掉全部四项（纯离线初始化：只配主机名/IP/存储/目录/SSH 密钥）
- 交互模式下会问这两件事，**直接回车 = 跳过**
- 只换源、没表态要不要更新时，**自动补一步 `apt update`**（新源配旧索引装包必 404），
  并在日志里说明这是自动补的；`--no-apt-update` 可显式否决
- 基础工具安装失败不再中断初始化（未刷新索引时多半是索引过期，会给出 `--apt-update` 提示）

### 2. 通路自检：防火墙与 SSH 通道

新增 `scripts/lib-network-audit.sh`（与 `lib-nodes.sh` / `lib-pydeps.sh` /
`lib-panel-host.sh` 同一约定：不 `set -e`、不定义 `log_*`、命令缺失时安全降级为
`unknown`）。bootstrap 启动时打印只读报告：

```
  ── 通路自检 (只读, 不修改任何设置) ──
    会话      : SSH 远程 (来自 192.168.1.50)
    sshd 端口 : 22
    防火墙    : iptables 生效中 (INPUT 默认策略 DROP)
    [高危]    : 当前规则未放行 SSH 端口 (22), 改网络配置或重启后可能直接失联
```

要点：**本仓库的脚本不主动改防火墙**（唯一会写 `iptables` 的是 wg-quick 的
PostUp/PostDown），报告只指出别人留下的坑：INPUT 策略 `DROP` 却没放行 SSH、
`ufw` / `firewalld` 启用后没放行、Docker 启动把 `FORWARD` 置 `DROP`
（本机是 WireGuard 出口时会顺带打断转发，报告会给出 `DOCKER-USER` 放行命令）。

改 IP 的自保动作：

- 改写 `network/` / `netplan/` 前先打快照（`/etc/onecloud/net-backup-<时间戳>/net-config.tar`）
- netplan 先 `netplan generate` 校验，**校验不过不 apply**（旧配置继续顶着）
- 通过 SSH 远端操作且 IP 将变更时，明确告知「这条连接会断」并打印重新登录与回滚命令
- 变更前风险预检新增两项：SSH 会话中改 IP、现有防火墙未放行 SSH 端口

WireGuard 规则同步修掉两个隐患：

| 问题 | 后果 | 现在 |
|------|------|------|
| `iptables -A` 无 `-C` 探测 | 反复 `wg-quick up` 规则越堆越多，`PostDown` 只删一条，残留难查 | 先 `-C` 再 `-A`（幂等） |
| `POSTROUTING -o eth0` 写死网卡 | 玩客云刷 Armbian 后网卡常是 `end0`，MASQUERADE 静默失效：wg 握手正常但客户端上不了网 | 节点侧探测默认路由网卡 |

### 3. 面板安装：IP / 端口 / 监听端口分开设置

`panel/install-service.sh` 现在接受参数（或交互询问，回车即默认）：

```bash
sudo bash panel/install-service.sh --host 192.168.1.101 --port 9000
sudo bash panel/install-service.sh --host 127.0.0.1 --port 9000 --url-port 19000
sudo bash panel/install-service.sh -y          # 全部默认, 不询问
```

| 参数 | 含义 |
|------|------|
| `--host` | **监听地址**（绑哪张网卡）：`0.0.0.0` / 本机局域网地址 / `127.0.0.1` |
| `--port` | **监听端口**（默认 9000） |
| `--url-host` | **访问地址**：面板 IP 或域名（用于生成访问入口，默认自动探测本机地址） |
| `--url-port` | **访问端口**：经 Nginx 反代 / 路由器端口映射 / `ssh -L` 时与监听端口不同 |

- 校验复用 `lib-panel-host.sh`：`192.168.1.0`、`127.0.0.0`、`224.0.0.1`、非法端口
  一律当场拒绝并给出可采用的替代值
- 监听/访问参数合并写入 `/etc/onecloud/panel.env`（600），只更新自己负责的四个键，
  **不会冲掉 `init.sh` 写入的账号密码**；unit 通过 `EnvironmentFile` 引用它
- `init/init.sh` 调用时传 `ONECLOUD_PANEL_TTY=0`，避免把已问过的问题再问一遍

---

## 🚀 v1.4.4 变更说明

**修复：面板监听地址写错（如 `127.0.0.0`）不会当场报错，要在 systemd 启动面板时
才以 `Cannot assign requested address` 失败。**

`PANEL_HOST` 此前是**自由文本、零校验**，填什么就写进 systemd unit。于是两类
高频误填都会漏到运行时：

| 误填 | 真实含义 | 后果 |
|------|---------|------|
| `127.0.0.0` | `127.0.0.0/8` 回环网段的**网络地址**（`127.0.0.1` 才是回环地址） | `bind()` 失败 |
| `192.168.1.0` | **网段地址** —— 想表达「同网段可访问」但写成了整个网段 | `bind()` 失败 |

### 交互：监听地址改为菜单选择

| 选项 | 取值 | 谁能访问 |
|------|------|---------|
| 1 | `0.0.0.0` | 所有网卡上的所有来源（含 WireGuard / 外网网卡，暴露面最大） |
| 2 | **本机局域网地址**（自动探测，推荐） | **同网段可直接访问**，其它网段需经路由/防火墙 |
| 3 | `127.0.0.1` | 仅本机（远端需 `ssh -L` 端口转发） |
| 4 | 手动输入其它 IPv4 | 走校验；非法值当场拒绝并给出替代值 |

> 「同网段可访问」的正确做法是绑**本机在该网段的地址**（如 `192.168.1.101`），
> 不是 `0.0.0.0` —— 后者会把 WireGuard 与外网网卡一起暴露出去。

手动输入 `192.168.1.0` 会被拒绝并提示改用本机在该网段的 `192.168.1.101`；
填合法的**非本机地址**会告警「面板启动会失败」并要求显式确认。配置汇总页标注
每个取值的访问范围（如「本机网卡地址 (同网段 192.168.1.0/24 可访问)」）。

### 三层防线（共用一套判定，避免实现漂移）

| 层 | 位置 | 作用 |
|----|------|------|
| 1 | `init/init.sh` | 交互引导 + 误填纠正（给出可采用的替代值） |
| 2 | `panel/install-service.sh` | 入参校验 —— 绕过 `init.sh` 直接调用也不放行 |
| 3 | `panel/app.py` | 启动前校验 —— 绕过前两层也拦得住 |

新增 `scripts/lib-panel-host.sh` 作为监听地址的**单一实现**（与 `lib-nodes.sh` /
`lib-pydeps.sh` 同样的约定：不 `set -e`、不定义 `log_*`）：

```bash
panel_detect_local_ipv4      # 本机可监听地址（ip → hostname -I → ifconfig 三级兜底）
panel_host_check 127.0.0.0   # 结果看 $PANEL_HOST_REASON / $PANEL_HOST_SUGGEST
panel_host_is_local 192.168.1.101
panel_host_desc 192.168.1.101   # "本机网卡地址 (同网段 192.168.1.0/24 可访问)"
panel_host_cidr 192.168.1.101   # 192.168.1.0/24
```

拦截范围：网段地址、广播地址、`127.0.0.0/8` 非 `.1` 地址、组播段（`224/4`）、
保留段（`240/4`）、链路本地（`169.254/16`）、`0/8` 保留（`0.0.0.0` 除外）、
非 IPv4 字面量（主机名 / IPv6）。

### 顺带修正

- 部署完成的访问地址按监听类型给出：`0.0.0.0` 列出各网卡真实入口（此前会打印
  `http://0.0.0.0:9000` 这种访问不了的地址），`127.0.0.1` 附 `ssh -L` 命令
- `install-service.sh` 启动后检查服务是否真的 active，不 active 时提示去确认地址

验证：新增第 23 组测试 31 项（静态接入 + mock 网卡下的 17 个取值判定 + 6 个交互
场景 + `install-service.sh` / `app.py` 两道防线的行为断言）。

---

## 🚀 v1.4.3 变更说明

**新增：`bootstrap.sh` 的 DNS 支持「DHCP 自动获取」，并把它作为默认值。**

原先 DNS 的兜底值是写死的 `1.1.1.1`（`lib-nodes.sh` 里同样如此）：家里路由器已经下发
DNS 的场景反被这个硬编码盖掉，而且没法表达「不干预、交给系统」。

### 取值方式

| 方式 | 写法 | 行为 |
|------|------|------|
| **自动获取（默认）** | `--dns dhcp` / `--dns auto` / `--dns none` / `--dns-dhcp`、清单 `dns: dhcp`、交互直接回车 | 不向系统写入任何 nameserver |
| 静态指定 | `--dns 223.5.5.5,1.1.1.1`、清单 `dns: 223.5.5.5`、`ONECLOUD_DNS=...` | 写入 `dns-nameservers` / netplan `nameservers` |

优先级：`--dns` > `ONECLOUD_DNS` > 清单 `network.dns` > 默认 `dhcp`。
配置确认页会标注 DNS 的取值来源（命令行 / 环境变量 / 清单 / 交互输入）。

### 具体行为

- **ifupdown**（`/etc/network/interfaces`）：自动获取时不写 `dns-nameservers`，只留一行说明注释
- **netplan**：自动获取时写成 `dhcp4: true` + `dhcp4-overrides`（`use-routes: false`、`use-ntp: false`）——
  **只从 DHCP 取 DNS，不用它下发的路由 / NTP**，静态地址与静态网关不受影响
- **交互式**：DNS 一定会问一次，直接回车 = 沿用候选值（候选为自动获取时回车即自动获取）
- **执行前告警**：自动获取模式下明确提示「脚本配的是静态 IP，若该机已无 DHCP 客户端在跑，
  解析可能失败，可改用 `--dns <地址>`」
- **WireGuard 不受影响**：`wg0.conf` 的 `DNS =` 必须是具体地址，清单写了 `dhcp` / `auto`
  标记时自动回退为 `1.1.1.1`（`wireguard-setup.sh`）
- **清单同步**：`inventory/nodes.yaml` 与 `nodes.local.yaml.example` 的 `network.dns`
  由 `1.1.1.1` 改为 `dhcp`
- 测试钩子 `ONECLOUD_ETC_ROOT` 覆盖到步骤 11（静态 IP 写入），
  便于在不触碰真实 `/etc` 的前提下做回归

⚠️ **注意**：玩客云配的是**静态 IP** —— 静态配置下不会跑 DHCP 客户端，`dhcp` 的语义是
「脚本不写死、交给系统现状」。若你的机器原本靠 DHCP 拿 DNS，改成静态 IP 后会失去这个来源，
此时请显式指定 `--dns`。

验证：新增第 22 组测试 22 项，覆盖取值归一化（dhcp/auto/none/`--dns-dhcp`）、
优先级（`--dns` > `ONECLOUD_DNS` > 清单）、交互回车默认、dry-run 预览，
并用探针实跑步骤 11 的 4 种组合（ifupdown / netplan × 自动获取 / 静态指定）核对写盘结果。

---

## 🚀 v1.4.2 变更说明

**修复：节点部署（`bootstrap.sh`）在 Debian 12 上以退出码 `100` 中断。**

现象是跑到「节点部署 → 本机初始化」后只看到一行 `[ERR] bootstrap 退出码: 100`，
没有任何可用的报错信息。根因有两条，叠加在一起：

| # | 问题 | 说明 |
|---|------|------|
| 1 | `sources.list` **写死 bullseye** | 机器实际是 Debian 12（bookworm）时被写入错误的代号，且 Debian 12 默认用 deb822 格式（`/etc/apt/sources.list.d/debian.sources`），再写一个 classic 文件等于两套源打架 |
| 2 | 基础工具里**无条件安装 `wireguard-dkms`** | 该包自 bookworm 起已从 Debian 移除（内核 5.6+ 已内置 wireguard 模块，本就无需 dkms）→ `E: Unable to locate package wireguard-dkms` |

`apt`/`apt-get` 出错时的退出码恒为 **100**，而脚本开头是 `set -e`，
于是这个 100 被原样透传出来，看不出是哪一步、为什么失败。

### 修复内容

- **源按系统实际代号渲染**：读 `/etc/os-release` 的 `VERSION_CODENAME`
  （回退 `lsb_release`），不再写死 bullseye；组件随代号自适应
  （bookworm 起见 `non-free-firmware`）
- **兼容 deb822**：系统已用 `/etc/apt/sources.list.d/debian.sources` 时就地重写该文件；
  其余仍指向 Debian 的源（含旧版脚本留下的 classic bullseye 行）会被注释掉，
  避免同一套源重复生效
- **改写前一律备份**：被改动的文件留 `<file>.onecloud.bak`
- **非 Debian 系自动跳过**：`ID` 不是 debian/armbian 时不动系统源，只告警
- **`wireguard-dkms` 改为按需安装**：内核 ≥ 5.6 直接跳过；
  更老的内核先 `apt-cache show` 探一下，源里确实有才装
- **apt 失败不再只有一个裸数字**：新增 `apt_run` 包装，失败时打印步骤名、
  完整命令、退出码含义（100 = 源不可达 / 包不存在 / dpkg 锁被占用 / 依赖冲突）
  以及源文件与系统代号，并**保留 apt 的原始报错**
- **`apt update` 仍为致命**（没索引后续装不了东西），但 **`apt upgrade` 降为不致命** ——
  玩客云常因内核/firmware 升级失败需重启，不该让整机初始化停在「主机名和源已改、
  其他什么都没配」的半成品状态

### 新增环境变量

| 变量 | 作用 |
|------|------|
| `ONECLOUD_APT_MIRROR` | 覆盖默认 Debian 镜像（默认清华 TUNA） |
| `ONECLOUD_APT_SECURITY_MIRROR` | 覆盖 security 镜像 |
| `ONECLOUD_DEBIAN_CODENAME` | 强制指定源代号 |
| `ONECLOUD_APT_SKIP_MIRROR=1` | 完全不换源，沿用系统原有源 |

### 已踩坑的机器怎么救

```bash
. /etc/os-release; echo "$PRETTY_NAME / codename=$VERSION_CODENAME"
sudo sed -i "s/bullseye/$VERSION_CODENAME/g" /etc/apt/sources.list   # 先把被污染的源改回来
sudo apt update && sudo apt install -y wireguard-tools               # 不要 wireguard-dkms
```

验证：新增第 21 组测试 27 项，提取 helper 与步骤 3~5 用 mock `apt`/`apt-cache`/`uname`
实测 10 个场景（bookworm 正常 / update 100 / upgrade 100 不致命 / 老内核有包无包两种 /
deb822 布局 / 非 Debian / 跳过换源 / bullseye / 包真缺失），并静态校验不得再出现
「写死 bullseye」与「无条件装 wireguard-dkms」。全量套件 259 → **286 项**，
全部通过、0 失败 0 警告。

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
