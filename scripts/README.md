# 🔧 scripts/ — 运维脚本

集群运维的核心工具集。所有脚本均支持 `-h`/`--help` 查看完整用法。

---

## 脚本一览

| 脚本 | 用途 | 常用命令 |
|------|------|---------|
| `lib-nodes.sh` | 节点清单库（被其他脚本 source） | — |
| `gen-panel-config.sh` | 从清单生成面板 config.json | 直接运行 |
| `gen-node-env.sh` | 从清单渲染各节点 .env | `[节点名]` / `--dry-run` |
| `bootstrap.sh` | 新节点初始化 | `--node <名> --yes` |
| `setup.sh` | 统一安装（交互式多选） | `sudo bash setup.sh` |
| `wireguard-setup.sh` | WireGuard mesh 配置 | `gen` / `add peer` / `list` |
| `deploy.sh` | rsync 分发配置到节点 | `-n <节点>` / `--exec <CMD>` |
| `install-services.sh` | 安装原生二进制或启动容器 | `mihomo` / `edge` / `all-native` |
| `health-check.sh` | 集群健康巡检 | 直接运行 |
| `backup.sh` | 备份配置与数据 | `all` / `config` / `node <名>` |
| `restore.sh` | 从备份恢复 | `<ID> <目标>` / `latest all` |
| `update-all.sh` | 批量更新镜像/系统包 | `-d` / `-s` / `-a` |

---

## 依赖关系

```
lib-nodes.sh          ← 所有运维脚本的底层依赖（自动 source）
    ├── inventory/nodes.yaml     节点数据源
    ├── inventory/nodes.local.yaml  本地覆盖（可选）
    └── inventory/services.yaml    服务映射

gen-panel-config.sh   → panel/config.json
gen-node-env.sh       → node-*/.env
```

---

## 详细说明

### lib-nodes.sh — 节点清单库

所有脚本的单一数据源。提供以下函数：

```bash
source scripts/lib-nodes.sh

node_ip wk-edge-01           # 获取 IP
node_hostname wk-edge-01     # 获取主机名
node_wg_ip wk-edge-01        # 获取 WireGuard IP
node_names                   # 列出所有节点名
node_resolve edge-01         # 简写 → 标准名
node_name_by_role edge-gateway  # 按角色查节点
node_of_service homeassistant   # 服务 → 节点映射
```

### bootstrap.sh — 节点初始化

在新刷好 Armbian 的玩客云上运行一次：

```bash
# 全部取清单默认值
./scripts/bootstrap.sh --node wk-edge-01 --yes

# 覆盖 IP 和主机名
./scripts/bootstrap.sh --node wk-edge-01 --ip 10.0.0.5 --hostname edge-hk --yes
```

功能：设置主机名、换国内源、更新系统、安装工具、创建 swap、挂载 SD 卡、迁移 Docker 数据、配置静态 IP、配置 hosts。

### deploy.sh — 配置分发

```bash
./scripts/deploy.sh                      # 分发所有节点
./scripts/deploy.sh -n wk-edge-01        # 仅分发到边缘网关
./scripts/deploy.sh -t                   # 测试 SSH 连接
./scripts/deploy.sh -d                   # 预览传输内容
./scripts/deploy.sh --exec "docker-compose up -d"  # 分发后启动服务
```

### setup.sh — 统一安装

交互式多选菜单，支持端口冲突检测、U 盘/SD 卡挂载：

```bash
sudo bash setup.sh
```

### wireguard-setup.sh — WireGuard 配置

```bash
./scripts/wireguard-setup.sh gen                    # 生成全部节点配置
./scripts/wireguard-setup.sh list                   # 查看已登记节点
./scripts/wireguard-setup.sh add peer wk-new 192.168.1.104 10.8.0.104
```

### install-services.sh — 服务安装

```bash
# 原生二进制
./scripts/install-services.sh mihomo       # Clash Meta
./scripts/install-services.sh xiaomusic    # 小米音乐
./scripts/install-services.sh migpt        # AI 助手
./scripts/install-services.sh all-native   # 全部原生

# Docker 服务
./scripts/install-services.sh edge         # NODE-01 全部容器
./scripts/install-services.sh iot          # NODE-02 全部容器
./scripts/install-services.sh storage      # NODE-03 全部容器
./scripts/install-services.sh all-docker   # 全部容器
```

### health-check.sh — 健康巡检

检查 SSH 连接、容器状态、端口监听、系统负载、OOM 记录、WireGuard Mesh、Syncthing 同步：

```bash
./scripts/health-check.sh
```

### backup.sh / restore.sh — 备份恢复

```bash
# 备份
./scripts/backup.sh all                    # 全量备份
./scripts/backup.sh config                 # 仅配置
./scripts/backup.sh node wk-edge-01        # 指定节点
./scripts/backup.sh service homeassistant  # 指定服务

# 恢复
./scripts/restore.sh latest all            # 恢复最新备份
./scripts/restore.sh 20260814_030000 node wk-iot-02
./scripts/restore.sh latest service homeassistant
```

备份目录：`/mnt/sd/backups`（可通过 `BACKUP_DIR` 环境变量自定义）

### update-all.sh — 批量更新

```bash
./scripts/update-all.sh                    # 更新 Docker 镜像（默认）
./scripts/update-all.sh -s                 # 更新系统包
./scripts/update-all.sh -a                 # 全部更新
./scripts/update-all.sh -n wk-edge-01      # 指定节点
```

注意：不会升级 Docker Engine（已 apt-mark hold）。

### gen-panel-config.sh / gen-node-env.sh — 配置生成

```bash
./scripts/gen-panel-config.sh              # 生成 panel/config.json
./scripts/gen-node-env.sh                  # 渲染各节点 .env
./scripts/gen-node-env.sh --dry-run        # 预览不写入
./scripts/gen-node-env.sh wk-edge-01       # 仅渲染指定节点
```
