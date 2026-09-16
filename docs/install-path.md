# 安装路径自适应（SD 卡 → /opt 回退）

> 适用阶段：系统初始化（`bootstrap.sh`，首次启动）与功能安装（`setup.sh`）。
> 目标机玩客云是无图形界面 / 1GB 内存 / eMMC 的无头服务器，多数节点会插 SD 卡扩容，
> 但「无卡 / 未挂载 / 挂载只读 / 空间不足」都是**正常状态**，绝不能因此阻断初始化或安装。

## 决策流程

```
探测 SD 卡设备 (lsblk /sys/block)
        │
        ├─ 未检测到设备 ──────────────┐
        │                            │
        ▼                            │
定位分区 (/dev/<dev>p1)             │
        │                            │
        ├─ 无可用分区 ───────────────┤
        │                            │
        ▼                            │
sd_mount_state(): 运行时查询挂载点   │
  (findmnt --source / mountpoint)   │
        │                            │
        ├─ 未挂载 ───────────────────┤
        │                            │
        ▼                            │
sd_rw_ok(): 真实创建+写入+删除探针   │
        │                            │
        ├─ 只读 ─────────────────────┤
        │                            │
        ▼                            │
sd_space_ok(): 可用空间 ≥ SD_MIN_SPACE_MB (默认 512MB)
        │                            │
        ├─ 空间不足 ─────────────────┤
        │                            │
        ▼                            ▼
   安装到 SD 卡挂载点          回退到 /opt/onecloud
   (DATA_ROOT = 挂载点)        (DATA_ROOT = /opt/onecloud)
```

## 关键性质

1. **检测顺序严格分级**：设备 → 分区 → 是否已挂载 → 是否可读写 → 剩余空间，逐级短路。
2. **SD 卡永远不是硬性前置**：任意一级不满足都安全降级到 `/opt/onecloud`，流程不中断。
3. **挂载路径运行时查询，不写死**：挂载点一律通过 `findmnt --source <设备>` / `mountpoint`
   查询得到，脚本里没有 `/mnt/sd` 这样的写死常量（旧的 `DATA_ROOT="/mnt/sd"` 已移除）。
4. **写入过程异常回退**：`safe_install_dir` / `safe_install_file` 在往 SD 写目录或文件失败时，
   自动把 `DATA_ROOT` 切回 `/opt/onecloud` 并重试，同时输出 `[ERROR]`/`[WARN]` 明确状态提示。
5. **可覆盖**：`INSTALL_FALLBACK_ROOT`（回退根）、`SD_MIN_SPACE_MB`（最小可用空间）均为环境变量，
   默认 `/opt/onecloud` 与 `512`。

## 组件路径映射（`install_path_for`）

| 组件 | 路径（以 `DATA_ROOT` 为基） |
|------|------|
| `srv`       | `$DATA_ROOT/srv`        |
| `docker`    | `$DATA_ROOT/docker`     |
| `backups`   | `$DATA_ROOT/backups`    |
| `scripts`   | `$DATA_ROOT/scripts`    |
| `docs`      | `$DATA_ROOT/docs`       |
| `inventory` | `$DATA_ROOT/inventory`  |

## 实现位置

- `scripts/lib-install-path.sh`：决策核心库（可单独 `source`、可单测）。
  关键函数：`sd_probe` / `sd_mount_state` / `sd_rw_ok` / `sd_space_ok` /
  `sd_evaluate` / `resolve_data_root` / `install_path_for` /
  `safe_install_dir` / `safe_install_tree` / `safe_install_file`。
- `scripts/bootstrap.sh` 第 14 步：调用 `resolve_data_root` + `safe_install_tree` 建服务目录。
  `--no-sd` 时直接固定回退 `/opt/onecloud`（不走 SD 探测）。
- `scripts/setup.sh` 全局配置段：启动即 `resolve_data_root`，`DATA_DIR` 取自 `install_path_for srv`。

## 测试

`test_validate.py` 第 30 组「安装路径自适应（SD卡→/opt 回退）」12 项断言覆盖：
SD 可用 / 无设备 / 未挂载 / 写入失败降级 / 空间不足 / 组件路径映射 / 静态接入点检查。
