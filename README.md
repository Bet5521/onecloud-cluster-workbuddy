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
# 访问 http://192.168.1.101:9000
```

面板已启用 HTTP Basic Auth 与命令白名单，默认账号 `admin` / `changeme`，
请通过环境变量覆盖：

```bash
PANEL_USER=admin PANEL_PASS='强密码' PANEL_PORT=9000 python3 app.py
```

---

## 🔧 运维脚本

| 脚本 | 作用 | 常用用法 |
|------|------|---------|
| `bootstrap.sh` | 新节点初始化（主机名/源/swap/SD卡/Docker/静态IP） | `--node <名> --ip <IP> --yes` |
| `setup.sh` | 统一安装（端口检测 + 多选批量安装 + 磁盘挂载） | `sudo bash setup.sh` |
| `wireguard-setup.sh` | WireGuard mesh 配置生成 | `gen` / `add peer` / `list` |
| `deploy.sh` | rsync 分发配置到各节点 | `-n <节点>` / `--exec <命令>` / `-t` / `-d` |
| `install-services.sh` | 安装原生二进制或启动节点容器 | `mihomo` / `edge` / `all-native` |
| `health-check.sh` | 集群健康巡检（SSH/容器/端口/负载/OOM） | 直接运行 |
| `backup.sh` | 备份配置与数据 | `all` / `config` / `node <名>` / `service <名>` |
| `restore.sh` | 从备份恢复 | `<备份ID> <目标>` 或 `<备份ID> service <名>` |
| `update-all.sh` | 批量更新镜像/系统包（不升 Docker Engine） | `-d` / `-s` / `-a` / `-n <节点>` |

所有脚本均支持 `-h` / `--help` 查看完整用法。

---

## ✅ 功能验证

项目自带验证套件，覆盖配置完整性、脚本语法、节点映射、服务一致性与
**文档化 CLI 接口契约**（共 176 项）：

```bash
python3 test_validate.py
```

输出示例：

```
  总计: 176 项
  通过: 176
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
