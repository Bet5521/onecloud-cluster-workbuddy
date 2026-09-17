# 🔧 scripts/ — 运维脚本

集群运维的核心工具集。所有脚本均支持 `-h`/`--help` 查看完整用法。

---

## 脚本一览

| 脚本 | 用途 | 常用命令 |
|------|------|---------|
| `lib-nodes.sh` | 节点清单库（被其他脚本 source） | — |
| `lib-install-path.sh` | 安装路径决策库（SD 卡判定与 `/opt/onecloud` 回退，被 bootstrap/setup source） | 直接 source |
| `lib-pydeps.sh` | Python 依赖安装库（pip 缺失多路降级，被 init/setup/install-services source） | 直接 source |
| `lib-panel-host.sh` | 面板监听地址库（探测本机网卡、校验绑定地址，被 init/install-service source） | 直接 source |
| `lib-network-audit.sh` | 通路/防火墙/SSH 自检库（只读探测，被 bootstrap source） | 直接 source |
| `gen-panel-config.sh` | 从清单生成面板 config.json | 直接运行 / `--out <文件或目录>` |
| `gen-node-env.sh` | 从清单渲染各节点 .env | `[节点名]` / `--dry-run` |
| `sync-panel-config.sh` | 把清单节点信息刷新到面板实际读取的 config.json | `--restart` / `--dry-run` |
| `fix-perms.sh` | 批量修复脚本执行权限（git 拉取后丢了 +x） | 直接运行 / `--list` / `--dry-run` |
| `bootstrap.sh` | 新节点初始化 | `--node <名> --yes` |
| `setup.sh` | 统一安装（交互式多选） | `sudo bash setup.sh` |
| `wireguard-setup.sh` | WireGuard mesh 配置 | `gen` / `add peer` / `list` |
| `deploy.sh` | rsync 分发配置到节点 | `-n <节点>` / `--exec <CMD>` |
| `install-services.sh` | 安装原生二进制或启动容器 | `mihomo` / `edge` / `all-native` |
| `health-check.sh` | 集群健康巡检 | 直接运行 |
| `backup.sh` | 备份配置与数据 | `all` / `config` / `node <名>` |
| `restore.sh` | 从备份恢复 | `<ID> <目标>` / `latest all` |
| `update-all.sh` | 批量更新镜像/系统包 | `-d` / `-s` / `-a` |
| `sd-tools.sh` | SD 卡工具箱入口（迁移/更换/格式化） | `migrate` / `replace` / `format` |
| `sd-format.sh` | SD 卡分区格式化为 ext4（根盘拦截） | `--dev <名>` / `--force-fmt` / `--dry-run` |
| `sd-migrate.sh` | eMMC(/opt) → SD 完整迁移 | `--yes` / `--dry-run` / `--clean` |
| `sd-replace.sh` | SD → USB 打包备份（换卡前） | `--yes` / `--dry-run` |

---

## 依赖关系

```
lib-nodes.sh          ← 所有运维脚本的底层依赖（自动 source）
    ├── inventory/nodes.yaml     节点数据源
    ├── inventory/nodes.local.yaml  本地覆盖（可选）
    └── inventory/services.yaml    服务映射

lib-pydeps.sh         ← Python 依赖安装的单一实现（自动 source）
    ├── init/init.sh                 面板依赖
    ├── scripts/setup.sh             migpt / panel 依赖
    └── scripts/install-services.sh  migpt 依赖

lib-panel-host.sh     ← 面板监听地址的单一实现（自动 source）
    ├── init/init.sh                 监听地址选择与校验
    └── panel/install-service.sh     注入 PANEL_HOST 时再校验一次

lib-install-path.sh   ← 安装路径决策的单一实现（自动 source）
    ├── scripts/bootstrap.sh         步骤 14 决定 DATA_ROOT（SD 挂载点 或 /opt/onecloud）
    └── scripts/setup.sh             install_path_for srv -> DATA_DIR / COMPOSE_DIR

lib-network-audit.sh  ← 通路/防火墙/SSH 自检库（只读，不 source 别的库）
    └── scripts/bootstrap.sh         环境自检段

gen-panel-config.sh   → panel/config.json
gen-node-env.sh       → node-*/.env
sync-panel-config.sh  → 刷新 panel/ 仓库副本 + systemd 指向目录 + /opt/onecloud/panel
```

> **数据根取值唯一入口**：远程路径一律经 `lib-nodes.sh: node_data_root <IP|节点名>`
> （SSH 读 `/etc/onecloud/install.conf` 的 `DATA_ROOT`，取不到回退 `ONECLOUD_REMOTE_DATA_ROOT`
> 或 `/mnt/sd`）。`deploy` / `backup` / `restore` / `update-all` / `health-check` 必须用它。
> `<DATA_ROOT>/srv/<完整节点名>` 是服务数据目录的约定布局。

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

### lib-pydeps.sh — Python 依赖安装库

所有 Python 依赖安装的**单一实现**。init / setup / install-services 都 source 它，
不再各自拼 `pip3 install`。

它解决的现实故障（Debian 12+ / Armbian 玩客云上很常见）：

```
python3 exists  →  but pip module missing
$ python3 -m pip install -r panel/requirements.txt
/usr/bin/python3: No module named pip
[WARN] 常规安装失败, 尝试 --break-system-packages (Debian 12+ / PEP 668)
/usr/bin/python3: No module named pip        ← 加了旗标也没用
[ERROR] Python 依赖安装失败
```

**`--break-system-packages` 只是 pip 的旗标**（用于绕过 PEP 668 的
`externally-managed-environment` 限制），**补不了缺失的 pip 自身**。

所以本库按「由轻到重」四路降级，任一路成功**且 import 校验通过**才算成功：

| 路线 | 手段 | 适用 |
|---|---|---|
| 1 | `pip install` | 已有 pip |
| 2 | `pip install --break-system-packages` | Debian 12+ 的 PEP 668 限制 |
| 3 | `ensurepip` → `apt python3-pip` → `get-pip.py` | pip 缺失，先补 pip 再回路线 1/2 |
| 4 | `apt install python3-flask python3-flask-cors` | pip 彻底不可用，完全绕开 pip |

```bash
source scripts/lib-pydeps.sh

pydeps_pick_python                                  # python3 / python
pydeps_pip_usable python3                           # pip 模块是否真的可用
pydeps_try_ensurepip python3                        # 单独补 pip 的三条子路线
pydeps_try_apt_pip ""
pydeps_try_getpip python3 ""
pydeps_ensure_pip python3 ""                        # 上面三条串起来
pydeps_verify python3 flask flask_cors              # 断言模块可 import
pydeps_install python3 "flask flask-cors" ""        # 主入口（自动降级 + 校验）
pydeps_install_from_file python3 panel/requirements.txt ""
pydeps_hint "flask flask-cors" python3 ""           # 失败时给人可复制的命令
```

要点：

- **不轻信安装命令的退出码** —— 每路都跟一次 `import` 校验，pip 谎报成功也会继续降级
- **依赖必须装进系统解释器**，不能用 venv：面板 systemd 单元执行的是 `/usr/bin/python3`
- **只定义函数、不产生副作用**，`source` 即零操作；需要 root 的命令走调用方传入的 `SUDO`
- **不定义 `log_*`**（各调用方命名不同），进度信息走 stderr，把 stdout 留给数据
- 需要系统改动的步骤（装 `python3-pip`、装发行版包）在 `init.sh` 里**都会先问**，
  以符合「纯交互、不预设默认」的约定

### lib-panel-host.sh — 面板监听地址库

面板 `PANEL_HOST` 的**单一实现**：探测本机网卡 + 校验绑定地址。`init.sh`
（交互引导）与 `panel/install-service.sh`（入参防线）都 source 它。

监听地址写错的代价很高：填成**网段地址**或**回环网段的网络地址**时不会当场报错，
要等 systemd 拉起 `app.py` 才以 `Cannot assign requested address` 失败，
日志里看不出真正原因。所以下面这些取值在校验阶段就被挡下：

| 取值 | 问题 | 给出的替代值 |
|---|---|---|
| `127.0.0.0` | 回环网段的**网络地址**（不是 `127.0.0.1`） | `127.0.0.1` |
| `192.168.1.0` | **网段地址** —— 面板只能绑到某台主机的地址 | 本机在该网段的地址（如 `192.168.1.101`） |
| `192.168.1.255` | 广播地址 | 同上 |
| `224.0.0.1` / `240.0.0.1` | 组播段 / 保留段 | 本机地址 |
| `169.254.x.x` | 链路本地（APIPA） | 本机地址 |
| `0.1.2.3` | `0/8` 保留（只有 `0.0.0.0` 合法） | `0.0.0.0` |
| `abc` / `::1` / `192.168.1.256` | 非 IPv4 字面量 | 本机地址 |

```bash
source scripts/lib-panel-host.sh

panel_detect_local_ipv4              # 列出本机可监听的 IPv4（ip → hostname -I → ifconfig 三级兜底）
panel_host_check 127.0.0.0           # 校验；结果看两个变量
echo "$PANEL_HOST_REASON"            #   失败原因
echo "$PANEL_HOST_SUGGEST"           #   可直接采用的替代值
panel_host_is_local 192.168.1.101    # 是否在本机某张网卡上
panel_host_desc 192.168.1.101        # "本机网卡地址 (同网段 192.168.1.0/24 可访问)"
panel_host_cidr 192.168.1.101        # 192.168.1.0/24（网络地址 + 前缀）
```

要点：

- **想「同网段可访问」就绑本机在该网段的地址**，不要用 `0.0.0.0` ——
  后者会在**所有**网卡上监听（含 WireGuard 与外网网卡），暴露面更大
- **不校验就写盘**是这个库要消灭的问题：三层防线（`init.sh` 交互 / `install-service.sh`
  入参 / `app.py` 运行时）共用同一套判定，避免各处实现漂移
- 与 `lib-pydeps.sh` 同样的约定：**不 `set -e`、不定义 `log_*`**，只把结果写到
  stdout 与结果变量，由调用方决定怎么呈现

### bootstrap.sh — 节点初始化

在新刷好 Armbian 的玩客云上运行一次：

```bash
# 全部取清单默认值
./scripts/bootstrap.sh --node wk-edge-01 --yes

# 覆盖 IP 和主机名 (网关按同网段自动推导: 10.0.0.1)
./scripts/bootstrap.sh --node wk-edge-01 --ip 10.0.0.5 --hostname edge-hk --yes

# 网关不是 .1 时显式指定, 不会被推导覆盖
./scripts/bootstrap.sh --node wk-edge-01 --ip 10.0.0.5 --gateway 10.0.0.254 --yes

# 未指定 --ip: 自动探测本机当前 IP/网关并询问是否采用 (--yes 下自动采用)
./scripts/bootstrap.sh --node wk-new-04 --yes

# 只预览配置与风险, 不改系统 (含 SD 挂载计划)
./scripts/bootstrap.sh --node wk-edge-01 --ip 10.0.0.5 --dry-run

# 不采用本机探测值 (探测仍会执行, 仅用于风险提示)
./scripts/bootstrap.sh --node wk-edge-01 --ip 10.0.0.5 --no-detect --yes

# 不插 SD 卡 / 换挂载点且不自动挂载
./scripts/bootstrap.sh --node wk-edge-01 --ip 10.0.0.5 --no-sd --yes
./scripts/bootstrap.sh --node wk-edge-01 --ip 10.0.0.5 --sd-mount /mnt/data --no-sd-automount --yes

# 装包: 默认只装核心包; 可选工具按需显式开启
./scripts/bootstrap.sh --node wk-edge-01 --ip 10.0.0.5 --extra-pkgs --yes
./scripts/bootstrap.sh --node wk-edge-01 --ip 10.0.0.5 --extra-pkgs "vim htop" --yes
```

**装包范围（无头服务器）**

| 档位 | 包 | 默认 |
|---|---|---|
| 核心 | `curl` `git` `ca-certificates` `jq` `rsync` `parted` `wireguard-tools` | 装 |
| 可选 | `wget` `vim` `htop` `iotop` `net-tools` `dnsutils` `unzip` `dosfstools` `fdisk` `lsb-release` `gnupg` | 不装，`--extra-pkgs` 开启 |
| 桌面/图形 | `APT_GUI_DENY` 黑名单 45 项（桌面套件 / Xorg / 显示管理器 / 字体 / 浏览器 / 远程桌面…） | 永不装 |

桌面/图形包即便被显式列出也会被 `pkg_gui_filter()` 剔除并告警。
详见 `docs/package-trim.md`。

#### 安装路径自适应（SD 卡 → /opt 回退）

初始化（`bootstrap.sh` 第 14 步）与安装（`setup.sh` 全局配置）统一调用
`lib-install-path.sh` 的 `resolve_data_root()`：SD 卡设备存在且已挂载、可读写、空间充足
（默认 ≥ `SD_MIN_SPACE_MB=512`）时安装到其运行时查询得到的挂载点；否则自动回退 `/opt/onecloud`，
**不阻断流程**。写 SD 失败时 `safe_install_dir` / `safe_install_file` 自动降级到 /opt 并重试。
详见 `docs/install-path.md`。

**参数与询问的关系**

传入的参数一律直接生效；只有"未提供且无法从清单/探测推断"的项才在终端可用时询问
（顺序：节点名 → IP → 主机名 → DNS → 网关）。非交互环境请加 `--yes`，否则缺失项
直接报错而不是干等输入。

**IP / 网关取值优先级**

| 项目 | 优先级 |
|---|---|
| IP | `--ip` > 本机探测（询问 / `--yes` 自动采用）> 清单 |
| 网关 | `--gateway` > 由最终 IP 推导（网络地址+1）> 本机探测 > 清单 |
| 前缀 | 本机探测 > 清单 `lan_subnet` > 24 |
| DNS | `--dns` > `ONECLOUD_DNS` > 清单 `network.dns` > 默认 `dhcp`（自动获取） |

换了网段时网关会跟着变：与现网关不同网段则提示询问，`--yes` 下自动调整；
显式 `--gateway` 不参与推导，但网段对不上仍会告警。
配置确认页会标注每个值的来源（命令行 / 本机探测 / 清单 / 由IP推导 / 环境变量 / 交互输入）。

**DNS 默认「DHCP 自动获取」**（v1.4.3 起）

```bash
# 默认: 不写死 nameserver, 交给 DHCP / 网络管理器 / 系统现状
./scripts/bootstrap.sh --node wk-edge-01 --yes
./scripts/bootstrap.sh --node wk-edge-01 --dns dhcp --yes     # 等价写法
./scripts/bootstrap.sh --node wk-edge-01 --dns auto --yes     # auto / none 同义
./scripts/bootstrap.sh --node wk-edge-01 --dns-dhcp --yes     # 不带值的开关

# 需要固定解析时显式给出 (多个用逗号分隔)
./scripts/bootstrap.sh --node wk-edge-01 --dns 223.5.5.5,1.1.1.1 --yes
ONECLOUD_DNS=8.8.8.8 ./scripts/bootstrap.sh --node wk-edge-01 --yes
```

- **ifupdown**（`/etc/network/interfaces`）：自动获取时不写 `dns-nameservers`，
  只留一行说明注释；静态指定时照旧写入
- **netplan**：自动获取时写成 `dhcp4: true` + `dhcp4-overrides`
  （`use-routes: false` / `use-ntp: false`），即**只从 DHCP 取 DNS，不用它下发的路由/NTP**，
  静态地址与静态网关保持不变
- 交互式运行时 DNS 一定会问一次，**直接回车 = 沿用当前候选值**（候选为自动获取时回车即自动获取）
- 脚本配的是**静态 IP**：若该机已无 DHCP 客户端在跑，自动获取会拿不到 DNS。
  执行前会明确告警，遇到解析异常改用 `--dns <地址>` 即可
- `wireguard-setup.sh` 不受影响：`wg0.conf` 的 `DNS =` 必须是具体地址，
  清单写了 `dhcp`/`auto` 标记时会回退为 `1.1.1.1`

**apt 源按系统实际代号渲染**（v1.4.2 起）

源不再写死 `bullseye` —— 读 `/etc/os-release` 的 `VERSION_CODENAME`（回退
`lsb_release`），组件随代号自适应（bookworm 起含 `non-free-firmware`）。
系统已用 deb822 格式（`/etc/apt/sources.list.d/debian.sources`）时就地重写该文件，
其余仍指向 Debian 的源会被注释掉以免重复；**被改动的文件都留 `.onecloud.bak`**。

| 环境变量 | 作用 |
|---|---|
| `ONECLOUD_APT_MIRROR` / `ONECLOUD_APT_SECURITY_MIRROR` | 覆盖默认镜像（默认清华 TUNA） |
| `ONECLOUD_DEBIAN_CODENAME` | 强制指定源代号 |
| `ONECLOUD_APT_SKIP_MIRROR=1` | 完全不换源，沿用系统原有源 |

`wireguard-dkms` 仅在**老内核且源里确实有**时才装（内核 ≥ 5.6 已内置 wireguard）。
`apt update` 失败即中断；`apt upgrade` 失败只告警继续，失败时都会打印
步骤名、完整命令、退出码含义（100 = 源不可达 / 包不存在 / dpkg 锁 / 依赖冲突）
与源文件位置，并保留 apt 原始报错。

**启动即探测**：脚本在解析参数前先读取本机当前 IP/前缀/默认网关与可移动存储，
用于网段比对和风险提示。

**SD 卡**：探测不到就跳过挂载与 Docker 数据迁移；探测到则询问是否挂载、挂载点
（默认 `/mnt/sd`）、是否写入 fstab 自动挂载。`--yes` 下按默认值自动执行。

**写入前的网络安全检查**：新 IP 与当前 IP 跨网段、目标 IP 已被占用、网关 ping
不通 —— 任一命中都会汇总告警；交互模式下需输入 `yes` 才继续。

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
./scripts/gen-panel-config.sh --out /opt/onecloud/panel   # 直接写到面板安装目录
./scripts/gen-node-env.sh                  # 渲染各节点 .env
./scripts/gen-node-env.sh --dry-run        # 预览不写入
./scripts/gen-node-env.sh wk-edge-01       # 仅渲染指定节点
```

### sync-panel-config.sh / fix-perms.sh — 初始化/部署修复（v1.5.5）

```bash
# 改完 inventory/nodes.yaml 或 nodes.local.yaml 后, 刷新面板**实际读取**的配置
./scripts/sync-panel-config.sh             # 刷新仓库副本 + 面板安装目录
./scripts/sync-panel-config.sh --restart   # 顺带重启 onecloud-panel
./scripts/sync-panel-config.sh --dry-run   # 只显示将写入的位置

# git 拉取后脚本丢了可执行位 -> 批量修复
./scripts/fix-perms.sh                     # 修复全部 *.sh
./scripts/fix-perms.sh --list              # 只列出不可执行的
./scripts/fix-perms.sh --dry-run           # 只显示将改动的
```

### 远程数据根（`/mnt/sd` vs `/opt/onecloud`）

`bootstrap.sh` 会把节点**实际**数据根写入 `/etc/onecloud/install.conf`；
`deploy.sh` / `backup.sh` / `restore.sh` / `update-all.sh` / `health-check.sh` 通过
`lib-nodes.sh` 的 `node_data_root()` 读取它（取不到回退 `/mnt/sd`），
因此「无 SD 卡、回退 `/opt/onecloud`」的节点也能正确分发 / 备份 / 恢复。
可用 `ONECLOUD_REMOTE_DATA_ROOT` 强制统一远程根（例如全部节点都用 `/opt/onecloud`）。

面板安装目录：`init/init.sh` 默认装到 `/opt/onecloud/panel`
（`ONECLOUD_PANEL_INSTALL_DIR` 覆盖；置空则沿用旧的「就地运行」）。
详见 [docs/init-deploy-fixes.md](../docs/init-deploy-fixes.md)。
