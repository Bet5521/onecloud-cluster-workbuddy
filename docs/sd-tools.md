# 💾 SD 卡工具箱（sd-format / sd-migrate / sd-replace / sd-tools）

OneCloud 集群运行在无图形界面的无头设备上，组件默认安装在 eMMC 的
`/opt/onecloud`（见 [install-path.md](install-path.md) 的「安装路径自适应」）。
当插入 SD 卡后，需要把数据落到 SD 卡以获得更大容量；更换 SD 卡前又需要把数据备份出来。
本工具箱提供四个脚本，覆盖 **格式化 → 迁移 → 更换备份** 的完整生命周期。

---

## 1. 总览

| 脚本 | 职责 | 典型触发 |
|------|------|----------|
| `scripts/sd-format.sh` | 将 SD 卡整卡分区并格式化为单一 ext4 分区 | 迁移/更换检测到「SD 卡非 ext4」时**自动触发**；也可手动执行 |
| `scripts/sd-migrate.sh` | 把无 SD 卡期间装在 eMMC 的组件/配置/依赖**完整迁移**到已挂载 SD 卡 | 插入 SD 卡后手动执行；也可经 `sd-tools.sh` 菜单进入 |
| `scripts/sd-replace.sh` | 更换 SD 卡前，把 SD 卡全部内容**打包压缩并转存到 USB 设备** | 准备换卡前手动执行 |
| `scripts/sd-tools.sh` | 交互式入口：菜单选择 1 迁移 / 2 更换 / 3 格式化 / 0 退出；也可非交互透传 | 日常入口 |

四个脚本统一遵循：

- **运行时探测，不写死路径**：设备（`/dev/mmcblk1`）、分区、挂载点一律通过
  `lib-install-path.sh` 的 `sd_probe` / `findmnt` / `lsblk` 等运行时查询得到，
  不依赖 `/mnt/sd` 之类的硬编码。
- **与安装路径决策引擎一致**：就绪判定复用 `ensure_sd_ready`
  （探测 → 必要时格式化 ext4 → 挂载 → 评估可读写/空间）。
- **支持 `--dry-run`**：先预演、后动手，避免误操作。
- **安全优先**：格式化只针对「可移动 SD 卡」，绝不格式化根磁盘；更换只在
  USB 设备满足空间与挂载条件时才动手。

> 前置依赖：`lib-install-path.sh`、`sd-format.sh`（迁移/更换会 source 它）、
> `rsync`、`parted`/`sfdisk` + `mkfs.ext4`（e2fsprogs）、`tar`。
> 无头服务器核心装包档（`BASE_PKGS`）已含 `rsync` 与 `parted`。

---

## 2. sd-format.sh —— SD 卡格式化

**职责**：将插入的 SD 卡（整卡）分区为单一 ext4 分区。供迁移/更换在
「检测到 SD 卡但文件系统非 ext4」时自动触发，也可独立手动执行。

**安全约束**

- 仅对探测到的「可移动 SD 卡」操作，**绝不格式化根磁盘**（通过 `_root_dev_name`
  比对拦截）。
- 已是 ext4 时默认跳过，除非 `--force-fmt`。
- 默认交互确认；`--yes` 跳过「会清空数据」的确认（危险，慎用）。

**用法**

```bash
./scripts/sd-format.sh [选项]
```

| 选项 | 说明 |
|------|------|
| `--dev <设备>` | 指定 SD 设备（如 `/dev/mmcblk1`），默认自动探测 |
| `--dry-run` | 只打印将要执行的分区/格式化操作，不实际执行 |
| `--yes` / `-y` | 非交互确认（跳过「会清空数据」提示） |
| `--force-fmt` | 即使已是 ext4 也强制重新格式化 |
| `-h` / `--help` | 显示帮助 |

独立执行示例：

```bash
# 自动探测并交互确认后格式化
sudo ./scripts/sd-format.sh
# 确认无误但想先看操作序列
sudo ./scripts/sd-format.sh --dry-run
```

---

## 3. sd-migrate.sh —— SD 卡迁移（eMMC → SD）

**场景**：初始化时未插 SD 卡，组件被安装到 eMMC 回退目录
`/opt/onecloud`。之后插入 SD 卡，本脚本把 eMMC 上的**全部**组件 / 配置 /
依赖文件完整迁移到 SD 卡对应目录。

**流程**

1. `ensure_sd_ready`：探测 SD → 非 ext4 自动格式化 → 挂载 → 评估（可读写/空间）。
2. 来源 = eMMC 回退目录（默认 `/opt/onecloud`，可用 `--source` 覆盖）。
   目标 = SD 卡实际挂载点（运行时查询）。
3. 交互确认（除非 `--force` / `--yes`）。
4. `migrate_data_root`：用 `rsync -aHAX` 把来源目录整体同步到 SD。
5. `migrate_docker`：若 Docker 数据仍位于 eMMC 默认位置 `/var/lib/docker`，
   则同步到 `<SD>/docker`，并改写 `/etc/docker/daemon.json` 的 `data-root`
   指向新位置，再重启 docker。
6. `verify`：重新 `resolve_data_root`，确认 `INSTALL_VIA_SD=1`（安装路径已切到 SD）。
7. 可选 `--clean`：校验通过后删除 eMMC 来源目录（默认**保留**，便于回退）。

**用法**

```bash
./scripts/sd-migrate.sh [选项]
```

| 选项 | 说明 |
|------|------|
| `--dry-run` | 只打印将要执行的操作（rsync 等），不真正迁移，也不破坏来源 |
| `--clean` | 迁移并校验通过后，删除 eMMC 上的来源目录（默认保留） |
| `--force` | 跳过「建议停服」提示 |
| `--yes` / `-y` | 非交互确认（与 `--dry-run` 配合可用于自动化） |
| `--source <目录>` | 指定 eMMC 来源根目录（默认 `/opt/onecloud`） |
| `-h` / `--help` | 显示帮助 |

**注意事项**

- **建议先停止相关容器/服务**再迁移，避免迁移过程中数据写入导致不一致；
  脚本默认会提示，可用 `--force` 跳过该提示（不推荐生产环境）。
- 迁移失败**不破坏来源**：`rsync` 失败时 eMMC 目录保持原样，可手动处理。
- `--dry-run` 完全只读，可用它先做预演。

---

## 4. sd-replace.sh —— SD 卡更换 / 备份到 USB（SD → USB）

**场景**：需要更换 SD 卡前，先把当前 SD 卡上的全部组件内容打包压缩，
转存到已插入的 USB 存储设备，以便换卡后恢复。

**前置（脚本自动校验，不通过则中止）**

- 已插入 USB 存储设备并挂载；
- USB 设备剩余空间 ≥ SD 卡数据量 + 预留余量（`SPACE_MARGIN_MB`，默认 512MB）；
- SD 卡已就绪（自动探测；非 ext4 自动格式化并挂载）。

**流程**

1. `ensure_sd_ready`：SD 卡探测 / 必要时格式化 ext4 / 挂载。
2. 计算 SD 内容大小（`du -sm` + 预留余量）。
3. `detect_usb`：用 `lsblk` 找「可移动 / 传输为 usb」的磁盘，排除 SD 卡自身
   与根文件系统，按 `findmnt` / `df` 选取第一个满足空间的挂载点。
4. 交互确认（除非 `--yes`）。
5. `pack`：以 `tar -czf … -C <sd_mp> .` 打包，归档名
   `onecloud-sd-backup-<主机名>-<时间戳>.tar.gz`，并输出大小与 `SHA256` 校验和。

**用法**

```bash
./scripts/sd-replace.sh [选项]
```

| 选项 | 说明 |
|------|------|
| `--usb <挂载点>` | 指定 USB 挂载点（默认自动探测第一个满足空间的 USB 设备） |
| `--dry-run` | 只打印将要执行的操作，不真正打包 |
| `--yes` / `-y` | 非交互确认 |
| `-h` / `--help` | 显示帮助 |

**注意事项**

- **必须先插入 USB 且空间足够**，否则脚本直接中止并给出明确提示；绝不会把
  根盘或 SD 卡自身误当 USB 目标。
- 归档为单文件 `tar.gz`，含 `SHA256` 校验和，便于换卡后在目标环境校验完整性。
- 大容量 SD 打包可能耗时，建议先用 `--dry-run` 预估。

---

## 5. sd-tools.sh —— 交互式入口

```bash
./scripts/sd-tools.sh            # 交互式菜单：1 迁移 / 2 更换 / 3 格式化 / 0 退出
./scripts/sd-tools.sh migrate  [参数]  # 透传到 sd-migrate.sh
./scripts/sd-tools.sh replace  [参数]  # 透传到 sd-replace.sh
./scripts/sd-tools.sh format   [参数]  # 透传到 sd-format.sh
./scripts/sd-tools.sh -h | --help
```

非交互模式下，参数会原样透传给对应脚本（例如
`./scripts/sd-tools.sh migrate --dry-run`）。

---

## 6. 测试覆盖

回归测试 `test_validate.py` 第 **31 组**「SD 卡工具箱（格式化/迁移/更换）」
共 **24 项**断言，覆盖：

- 脚本存在性与语法（4 个脚本 × 存在 + `bash -n` = 8 项）；
- 格式化：已为 ext4 不重复格式化、非 ext4 执行分区+格式化、拒绝格式化根磁盘、
  dry-run 不执行（4 项）；
- 迁移：无 SD 卡拒绝、检测到非 ext4 自动触发格式化并进入 dry-run 迁移、
  来源目录不被破坏、ext4 直接 dry-run 迁移（共 5 项）；
- 更换：未插 USB 拒绝、USB 空间不足拒绝、正常备份并生成非空归档（3 项）；
- 调度：`sd-tools` 帮助列出三功能、`migrate`/`format` 正确透传（3 项）。

运行方式（建议只跑该组验证）：

```bash
python test_validate.py          # 全量；SD 组为最后一组
```

---

## 7. 测试钩子（仅供 CI / 验证使用）

正常运维无需关心以下环境变量；它们仅在回归测试中用于注入虚拟设备/空间：

| 变量 | 作用 |
|------|------|
| `ONECLOUD_SD_TEST_DEV` | 指定虚拟 SD 设备名，跳过设备存在性检查 |
| `ONECLOUD_USB_TEST_PART` | 指定虚拟 USB 分区（配合 `OC_TEST_NO_SYSBLOCK=1`） |
| `ONECLOUD_USB_TEST_FREE_MB` | 直接给定 USB 可用空间（MB），绕过 `df` 实测 |
| `OC_TEST_NO_SYSBLOCK` | 关闭 `/sys/block` 探测，改用钩子给定设备 |
| `FMT_LOG` | 记录实际执行的分区/格式化命令（写入 `MKFS` / `DRYRUN` 标记） |
