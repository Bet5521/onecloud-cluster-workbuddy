# 🚀 init/ — 交互式初始化入口

集群部署与运维的**统一交互式控制台**。一个菜单进来，就能完成面板部署、节点初始化、
日常巡检、备份恢复、配置分发与服务安装。

```bash
bash init/init.sh
```

---

## 设计约定

| 约定 | 说明 |
|------|------|
| **纯交互** | `init.sh` 不接受任何命令行参数，传入参数会直接报错退出（`exit 2`） |
| **不预置默认参数** | 调用子脚本前一律先询问，由用户当场选择；不会替你补 `--yes` / `--ip` 之类的值 |
| **无隐式默认值** | 所有提问都要求显式输入（`y`/`n` 或具体值），不存在"回车即采用"的默认项 |
| **执行前可见** | 每次真正调用子脚本前，会把完整命令打印出来并要求再次确认 |
| **单一实现** | 本入口只做编排，不重复实现安装逻辑；底层全部复用 `scripts/` 与 `panel/` 中已有脚本 |

> 因为没有任何默认参数，本入口**不能用于自动化/CI**。批量与无人值守场景请直接调用
> `scripts/` 下的脚本并显式传参（见根 README「快速开始」）。

---

## 菜单结构

```
主菜单
├── 1) 部署 Panel 控制面板
│     ├── 部署为 systemd 常驻服务（开机自启）
│     ├── 仅前台试运行（Ctrl+C 结束，不写 systemd）
│     └── 仅安装 Python 依赖
├── 2) 部署节点（新节点初始化）
│     ├── 本机初始化（当前机器就是待部署节点，需 root）
│     ├── 打开远程节点的交互入口（SSH 接力）
│     └── 查看节点清单
├── 3) 节点维护
│     ├── 集群健康巡检
│     ├── 备份配置与数据
│     ├── 从备份恢复
│     ├── 批量更新镜像 / 系统包
│     └── 生成防火墙设置建议清单（静态生成，不改任何防火墙）
├── 4) 配置与分发
│     ├── 分发配置到节点（deploy.sh）
│     ├── 仅测试节点 SSH 连接
│     ├── 预览将要分发的文件
│     ├── 分发后在节点上远程执行命令
│     ├── 生成面板配置 panel/config.json
│     ├── 渲染节点 .env 文件
│     └── WireGuard 配置管理（gen / add peer / list）
├── 5) 服务安装
│     ├── 统一安装（setup.sh 交互多选 + 端口冲突检测）
│     ├── 安装原生服务（mihomo / xiaomusic / migpt / verysync）
│     └── 启动节点容器（edge / iot / storage / all-docker）
├── 6) 环境自检
└── 7) 退出
```

---

## 功能到脚本的映射

本入口不实现业务逻辑，全部转发给既有脚本：

| 菜单功能 | 实际调用 |
|----------|----------|
| 部署面板（systemd） | `panel/install-service.sh` + `/etc/onecloud/panel.env` + systemd drop-in |
| 部署面板（前台 / 装依赖） | `panel/app.py`、`panel/requirements.txt` |
| 本机节点初始化 | `scripts/bootstrap.sh`（**不带参数**，由 bootstrap 自己提问） |
| 集群健康巡检 | `scripts/health-check.sh` |
| 备份 / 恢复 | `scripts/backup.sh`、`scripts/restore.sh`（`BACKUP_DIR` 由菜单询问后传入） |
| 生成防火墙设置建议清单 | `scripts/firewall-recommend.sh`（只读清单静态推算，落到 `docs/firewall/`） |
| 批量更新 | `scripts/update-all.sh`（`-d` / `-s` / `-a`，由菜单选择） |
| 分发配置 / 测连通 / 预览 / 远程执行 | `scripts/deploy.sh` |
| 面板配置 / 节点 .env | `scripts/gen-panel-config.sh`、`scripts/gen-node-env.sh` |
| WireGuard | `scripts/wireguard-setup.sh`（`gen` / `add peer` / `list`） |
| 统一安装 | `scripts/setup.sh` |
| 原生服务 / 节点容器 | `scripts/install-services.sh` |

节点列表统一来自 `scripts/lib-nodes.sh`（即 `inventory/nodes.yaml`），本目录**不含任何硬编码 IP**。

---

## 面板部署说明

选「部署为 systemd 常驻服务」时，交互会依次要求输入：

1. 监听端口（必填，会做 1-65535 校验与占用检测）
2. 监听地址（菜单选择，见下）
3. 登录用户名（必填）
4. 登录密码（必填，输入不回显）

**监听地址怎么选** —— 想「同网段可访问」应绑**本机在该网段的地址**，
而不是 `0.0.0.0`（后者会在所有网卡上监听，含 WireGuard 与外网网卡）：

| 选项 | 取值 | 谁能访问 |
|------|------|---------|
| 1 | `0.0.0.0` | 所有网卡上的所有来源（暴露面最大，需配合防火墙） |
| 2 | 本机局域网地址（自动探测，如 `192.168.1.101`） | **同网段可直接访问**，其它网段需经路由/防火墙 |
| 3 | `127.0.0.1` | 仅本机（远端需 `ssh -L` 端口转发） |
| 4 | 手动输入其它 IPv4 | 逐项校验，见下 |

手动输入会走 `scripts/lib-panel-host.sh` 的校验，下面这些"看起来像地址、实际绑不上"
的取值会被**当场拒绝并给出可采用的替代值**（否则要等 systemd 拉起面板时
才以 `Cannot assign requested address` 失败）：

| 输入 | 拒绝原因 | 给出的替代值 |
|------|---------|------------|
| `127.0.0.0` | 回环网段的网络地址（`127.0.0.1` 才是回环地址） | `127.0.0.1` |
| `192.168.1.0` | 网络地址（整个网段）—— 面板只能绑到某台主机的地址 | 本机在该网段的地址 |
| `192.168.1.255` | 广播地址 | 本机在该网段的地址 |
| `224.0.0.1` / `169.254.1.1` | 组播段 / 链路本地段 | 本机地址 |
| `abc` / `::1` | 非 IPv4 字面量 | 本机地址 |

合法但**不在本机任何网卡上**的地址（如 `192.168.9.9`）会先告警
「面板启动会失败」，仍需你显式确认才会采用。配置汇总页会标注每个取值的访问范围
（例如「本机网卡地址 (同网段 192.168.1.0/24 可访问)」）。

随后写入两个文件，再复用 `panel/install-service.sh` 安装服务：

| 文件 | 作用 |
|------|------|
| `/etc/onecloud/panel.env` | 存放 `PANEL_HOST/PANEL_PORT/PANEL_USER/PANEL_PASS`，权限 `600` |
| `/etc/systemd/system/onecloud-panel.service.d/10-init-override.conf` | 通过 `EnvironmentFile` 把上面的变量注入服务 |

之所以用 drop-in 而不是重写 unit，是为了保持 `panel/install-service.sh` 是面板服务安装的
**唯一实现**，避免两份 unit 模板各自漂移。

调用安装脚本时会带上 `ONECLOUD_PANEL_TTY=0`：监听地址/端口已在本菜单问过，
不要再让底层脚本问一遍；**访问地址**（面板 IP 或域名）则由脚本按本机地址自动填充。
需要单独指定访问入口（反代 / 端口映射 / `ssh -L`）时，直接调用安装脚本：

```bash
sudo bash panel/install-service.sh --host 127.0.0.1 --port 9000 --url-host panel.lan --url-port 19000
```

访问地址与账号会在部署结束时打印。

> 面板默认部署到**运行本入口的这台机器**。若要把面板装到 edge 节点，请先 SSH 到该节点，
> 在项目目录里运行 `bash init/init.sh`，或从「部署节点 → 打开远程节点的交互入口」跳转过去。

---

## 前置条件

- **bash**（脚本用 `BASH_SOURCE` 定位项目根，请用 `bash init/init.sh` 而不是 `sh init/init.sh`）
- 节点相关操作需要 **SSH 免密**（约定 `root@<节点IP>`）与 **rsync**
- 面板部署需要 **python3**（依赖安装失败时会自动重试 `--break-system-packages`）
- 需 root 的操作（bootstrap、setup、install-services、systemd）在非 root 下会自动加 `sudo`

缺什么可以直接跑菜单里的 **6) 环境自检**，它会逐项列出缺失的目录、脚本与命令，
并单独报告 **Python 环境**：解释器路径、`pip` 模块是否可用、`flask`/`flask_cors`
是否可 import。面板依赖装不上时，先跑自检能立刻定位到「是缺 pip 还是缺包」。

---

## Python 依赖：pip 缺失时的降级链

Debian 12+ / Armbian 上常见「`python3` 在、`pip` 不在」，此时部署面板会报：

```
/usr/bin/python3: No module named pip
[WARN] 常规安装失败, 尝试 --break-system-packages (Debian 12+ / PEP 668)
/usr/bin/python3: No module named pip
[ERROR] Python 依赖安装失败
```

**`--break-system-packages` 只是 pip 的旗标**（绕过 PEP 668 的
`externally-managed-environment`），**补不了缺失的 pip 自身**。

`init.sh` 现在走 `scripts/lib-pydeps.sh` 的四路降级：pip → `--break-system-packages`
→ 补 pip（`ensurepip` / `apt python3-pip` / `get-pip.py`）→ `apt` 装发行版包
（`python3-flask python3-flask-cors`，完全绕开 pip）。
每一路之后都会实跑一次 `import` 校验，**pip 谎报成功也会继续降级**。

其中「用 apt 装 `python3-pip`」和「改用系统包」这两步**都会先问你**；
两步都拒绝且依赖确实缺失时，会打印可复制的兜底命令。

---

## 常见问题

**Q: 为什么提示"不接受任何命令行参数"？**
A: 这是刻意设计。本入口定位是纯交互，避免"以为传了参却走了另一条路径"。需要传参请直接用 `scripts/` 下的脚本。

**Q: 备份目录每次都要手输，很麻烦。**
A: 对的，这是「不采用默认参数」的直接结果。备份根目录由你当场指定，避免误写到他处。

**Q: 面板部署失败，服务起不来。**
A: 先看 `systemctl status onecloud-panel` 与 `journalctl -u onecloud-panel -n 50`；
常见原因是 `python3` 路径不是 `/usr/bin/python3`，或端口被占用。

---

## 相关文档

- [../README.md](../README.md) — 项目总览与快速开始
- [../scripts/README.md](../scripts/README.md) — 各运维脚本详解
- [../panel/README.md](../panel/README.md) — 面板功能与 API
- [../docs/operations.md](../docs/operations.md) — 运维手册
