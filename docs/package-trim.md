# 初始化装包精简说明

> 适用版本：v1.5.2
> 目标环境：**玩客云 WS1608（Amlogic S805 / ARMv7 / 1GB RAM / eMMC+SD），无图形界面**
> 涉及脚本：`scripts/bootstrap.sh`（节点初始化）、`scripts/setup.sh`（服务安装）

## 1. 背景与结论

`bootstrap.sh` 原先在初始化阶段一次性安装 18 个包。目标机是无显示输出、1GB 内存的
无头服务器，其中相当一部分包既不被部署链路调用，也不是系统运行所必需。

本次按三档重构：

| 档位 | 数量 | 默认行为 |
|---|---|---|
| 核心包 `BASE_PKGS` | 7 | 装 |
| 可选包 `OPT_PKGS_PRESET` | 11 | 不装，`--extra-pkgs` 显式开启 |
| 桌面/图形 `APT_GUI_DENY` | 45 项黑名单 | 永不装，显式列出也会被剔除并告警 |

**净效果：默认初始化从 18 个包降到 7 个。**

## 2. 保留的核心包及理由

| 包 | 保留理由（谁在用） |
|---|---|
| `curl` | `setup.sh` 下载 docker 安装脚本 / GitHub release / `get-pip.py`；`lib-pydeps.sh` 主下载通道 |
| `ca-certificates` | HTTPS 校验证书根；缺失时 `curl` 直接失败，等于断了所有下载 |
| `git` | 初始化后第一步就是克隆 `onecloud-cluster` 仓库到 `/mnt/sd/` |
| `jq` | `setup.sh` 解析 GitHub release JSON、面板接口返回 |
| `rsync` | 迁移 `/var/lib/docker` 到 SD 卡（bootstrap 第 9 步） |
| `parted` | SD 卡分区：`mklabel gpt` / `mkpart primary ext4`；U 盘挂载同样使用 |
| `wireguard-tools` | 提供 `wg` / `wg-quick`，三节点 WireGuard 组网的地基 |

未声明但系统必然自带的（Debian `Priority: required/important`），不重复安装：
`iproute2`（`ip`/`ss`）、`e2fsprogs`（`mkfs.ext4`）、`util-linux`（`mount`/`fdisk` 底层）、
`systemd`、`openssh-client`。

## 3. 被移出默认流程的组件（改为可选，未删除）

以下命令仍然可用，只是不再进入默认初始化：

```bash
./scripts/bootstrap.sh --node wk-edge-01 --yes --extra-pkgs            # 装下面全部
./scripts/bootstrap.sh --node wk-edge-01 --yes --extra-pkgs "vim htop" # 只装指定的
ONECLOUD_EXTRA_PKGS="vim htop" ./scripts/bootstrap.sh ...              # 等价环境变量
```

| 包 | 原用途 | 移出原因 |
|---|---|---|
| `wget` | 下载兜底 | `curl` 已覆盖全部下载路径；`lib-pydeps.sh` 的 `wget` 分支只是 `curl` 缺失时的兜底，常态不触发 |
| `vim` | 编辑器 | 改配置由脚本 `sed`/heredoc 完成；系统自带 `nano`，不需要为了初始化再装一个编辑器 |
| `htop` | 交互式进程监控 | 纯排障工具，不是部署依赖；面板（`panel/app.py`）已提供 CPU/内存/容器状态 |
| `iotop` | 交互式 IO 监控 | 同上，纯排障；且需要内核 IO accounting 支持，小盒子上常拿不到数据 |
| `net-tools` | `ifconfig`/`netstat`/`route` | 已被 `iproute2`（`ip`/`ss`）取代且官方停止维护；脚本走 `ip` 命令路径，`netstat` 只是兜底分支 |
| `dnsutils` | `dig`/`nslookup` | 排障工具；DNS 配置由 bootstrap 写文件完成，不调用 `dig` |
| `unzip` | 解压 zip | 全流程用 `docker` 与 `tar`/`git`，没有任何一步需要解压 zip |
| `dosfstools` | `mkfs.vfat`/`fsck.fat` | 格式化一律用 `mkfs.ext4`；挂载 vfat/exfat 由内核 + `mount` 负责，不需要该包 |
| `fdisk` | 分区 | `parted` 已覆盖全部分区操作（含 GPT）；`fdisk` 与 `parted` 功能重叠 |
| `lsb-release` | 取发行版代号 | `detect_distro()` 主路径读 `/etc/os-release`，`lsb_release` 只是取不到时的兜底；Armbian 上常缺失 |
| `gnupg` | 管理 apt 源密钥 | 初始化不加第三方 apt 源（Docker 走 `get.docker.com`），用不到 `apt-key`/`gpg` |

## 4. 桌面 / 图形组件：一律不安装

**结论：核查后确认，原脚本中并不存在任何桌面环境、显示服务或 GUI 应用。**
本次没有"已存在的 GUI 包需要删除"，但把这条约束固化成了机制，防止后续误加：

- 新增黑名单常量 `APT_GUI_DENY`（45 项）+ 判定函数 `pkg_gui_name()`
- 新增过滤函数 `pkg_gui_filter()`：装包前统一过一遍，命中即剔除并打印
  `已剔除桌面/图形组件 (目标机为无头服务器, 不安装): ...`
- 匹配双路：全名精确匹配 + 命名前缀/后缀（`xserver-*` / `x11-*` / `xorg-*` /
  `task-*-desktop` / `*-desktop` / `fonts-*`）

黑名单覆盖的类别：

| 类别 | 代表包 |
|---|---|
| 桌面套件 | `task-desktop`、`task-gnome-desktop`、`task-kde-desktop`、`task-lxde-desktop`、`task-xfce-desktop` |
| 显示服务 / X11 | `xorg`、`xserver-xorg`、`xserver-xorg-core`、`xinit`、`x11-common`、`x11-apps`、`x11-utils`、`x11-xserver-utils`、`xauth`、`xvfb`、`dbus-x11` |
| 显示管理器（登录界面） | `lightdm`、`gdm3`、`sddm`、`xdm`、`slim` |
| 桌面应用 / 文件管理器 | `gvfs`、`thunar`、`pcmanfm`、`nautilus`、`gedit`、`libreoffice`、`xterm` |
| 浏览器 | `chromium`、`chromium-browser`、`firefox-esr` |
| 远程桌面 / VNC | `x11vnc`、`tigervnc-standalone-server`、`xrdp` |
| 字体 | `fonts-noto-core`、`xfonts-base` 及全部 `fonts-*` |
| 音频 / 蓝牙 | `alsa-utils`、`pulseaudio`、`bluez`、`blueman` |

这类东西在 1GB ARMv7 盒子上的代价是实打实的：占 eMMC 空间、常驻内存、
引入大量依赖还可能拉入 `systemd` 图形 target，而机器连 HDMI 输出都不接。

## 5. `setup.sh` 一并收敛

| 位置 | 原行为 | 现在 |
|---|---|---|
| `ensure_tools()` | 缺什么补 `jq`/`curl`/`wget`/`parted`/`dosfstools` | 只补 `jq`/`curl`/`parted` |
| `check_port()` | `ss`/`netstat` 都没有时 `apt install net-tools` | 只告警，不再为此装包 |
| `ensure_pkg()` | 通用装包函数 | 已无引用，删除 |

`wget` 与 `dosfstools` 的移除理由同上表。`check_port` 的兜底分支实际不可达
（`ss` 来自 `iproute2`，必装），与其为假想场景拉入已停止维护的 `net-tools`，
不如明确告警。

## 6. 兼容性

- 旧开关全部保留：`--no-apt-pkgs`、`ONECLOUD_APT_SKIP_PKGS`、`ONECLOUD_APT_SKIP_ALL`
- `--no-apt` 与 `ONECLOUD_APT_SKIP_ALL=1` 现在也会一并关掉可选包
- 需要编辑器/排障工具的场景，一条 `--extra-pkgs` 即可恢复，不影响既有习惯

## 7. 回归测试

`test_validate.py` 第 29 组「初始化装包精简（无头服务器）」19 项，覆盖：

1. 三档常量齐备（`BASE_PKGS` / `OPT_PKGS_PRESET` / `APT_GUI_DENY`）
2. 默认清单与核心集合**精确相等**（多一个少一个都算失败）
3. 11 个可选包全部"改为可选"而非被删除
4. 核心包与可选包无交集
5. 核心/可选清单均不含桌面图形组件
6. `setup.sh` 不再默认补装 `wget` / `dosfstools` / `net-tools`
7. mock apt 行为验证：默认只装核心包、可选包零泄漏
8. `--extra-pkgs` 不带值装预设、带值只装指定的
9. 显式列出 `xorg` / `firefox-esr` 时被剔除，且**有告警**（不能静默丢弃）
