# 初始化 / 部署链路缺陷修复（v1.5.5）

> 本文记录初始化与部署过程中发现的四类「写入的值没有同步到实际使用处」缺陷，
> 覆盖：**脚本执行权限**、**面板文件落位**、**节点 IP → 面板配置同步**、**远程数据根一致**。
> 每项给出：现象 → 根因 → 修复 → 验证。

---

## 1. git 拉取后所有脚本丢失执行权限

### 现象
`git clone` / `git pull` 到 Linux 后执行 `./scripts/xxx.sh` 报 `Permission denied`。

### 根因
仓库把脚本以 **`100644`（无执行位）** 登记进 git 索引（`git ls-files -s` 可见）。
git 只记录「可执行 / 不可执行」一位；索引里是 644，克隆/拉取后就一定不是可执行文件。
在 Windows 上克隆（`core.fileMode=false`）尤其容易长期忽略此问题。

### 修复
1. **根治（索引层）**：把可执行位写回索引，此后克隆/拉取自带 `+x`：
   ```bash
   git update-index --chmod=+x $(git ls-files '*.sh')
   git commit -m "chore: 记录脚本可执行位"
   ```
2. **兜底（存量克隆 / 非 git 拷贝）**：新增 **`scripts/fix-perms.sh`**：
   - 默认：把仓库内所有 `*.sh` 恢复为可执行（幂等，已可执行的跳过）；
   - `--list`：只列出当前**不可执行**的脚本；
   - `--dry-run`：只显示将要 `chmod` 的文件，不改动；
   - `--with-py`：顺带给 `.py` 入口加执行位；
   - `--root DIR`：指定仓库根（默认脚本上级目录）。
3. **自愈**：`init/init.sh` 启动预检发现脚本缺执行位时，自动调用 `fix-perms.sh` 修复并提示。

### 验证
- `bash scripts/fix-perms.sh --list` / `--dry-run` / 修复三种模式均统计正确、返回 0；
  在含 1 个 `.sh` 的临时目录上统计为「1 个脚本」。
- 测试第 32 组断言脚本具备 `chmod +x`、`--list`、`--dry-run` 与 git 索引提示。

> 注：Windows（msys）下 `[ -x *.sh ]` 恒为真（按扩展名判定），因此该脚本在 Windows 上
> 通常报告「无需修改」——它真正生效的场景是 Linux 目标机。

---

## 2. panel 文件留在 git 克隆目录

### 现象
面板以 git 克隆路径为运行目录；该目录被移动、清理，或 `git pull` 覆盖后，面板服务失效。

### 根因
`panel/install-service.sh` 生成的 systemd 单元中：
```
WorkingDirectory=<git 克隆路径>/panel
ExecStart=/usr/bin/python3 <git 克隆路径>/panel/app.py
```
运行目录与源码目录是同一个 —— 源码一变，运行即受影响。

### 修复
`panel/install-service.sh` 新增 **`--install-dir DIR`**（环境变量等价：`ONECLOUD_PANEL_INSTALL_DIR`）：

- 指定后，把运行必需文件复制到稳定目录：
  `app.py`、`templates/`、`static/`、`requirements.txt`、`config.json`；
- systemd 单元的 `WorkingDirectory` / `ExecStart` / `Environment=PANEL_CONFIG` 均指向该目录；
- 校验库 `lib-panel-host.sh` 仍从**源码目录**（`SRC_DIR`）定位，与安装目录解耦；
- **默认行为不变**（不传 `--install-dir` 即就地运行，向后兼容）。

`init/init.sh` 默认安装到 **`/opt/onecloud/panel`**：
```bash
PANEL_INSTALL_DIR="${ONECLOUD_PANEL_INSTALL_DIR-/opt/onecloud/panel}"
```
- 设为**空字符串**（`ONECLOUD_PANEL_INSTALL_DIR=`）可退回旧的「就地运行」；
- 安装面板时通过 `sudo env ONECLOUD_PANEL_INSTALL_DIR=... bash panel/install-service.sh` 注入。

### 验证
- 抽取 `panel_install_copy` 在临时目录执行，`app.py` / `templates/` / `static/` /
  `config.json` / `requirements.txt` 均已就位；
- 测试第 32 组断言 `--install-dir`、`SRC_DIR` 解耦与 `init.sh` 注入。

---

## 3. 部署节点 / 面板时修改的节点 IP 未同步到面板配置

### 现象
面板监控不到节点，也无法对节点执行任何操作。

### 根因（两条链同时断）
1. **清单未回写**：节点 IP 的唯一数据源是 `inventory/nodes.yaml`（可用 `nodes.local.yaml` /
   环境变量覆盖）。`bootstrap.sh` 交互选定的 IP 只写进本机网络配置（netplan），
   **没有回写清单**，控制端读到的仍是旧 IP。
2. **写入位置不对**：面板读取的 `panel/config.json` 由 `gen-panel-config.sh` 从清单生成，
   但面板实际运行在 `/opt/onecloud/panel`（见问题 2），仓库里那份 `config.json` 不是它读的那份。

### 修复
| 环节 | 变更 |
|------|------|
| 节点侧固化 | `bootstrap.sh` 结尾把**节点名 / IP / 主机名 / WG IP** 回写 `inventory/nodes.local.yaml`（幂等 upsert：已存在就地更新字段，不存在则追加到 `nodes:` 段） |
| 生成器 | `gen-panel-config.sh` 新增 **`--out FILE_OR_DIR`**（等价 `ONECLOUD_PANEL_CONFIG`），可写到任意位置 |
| 同步器 | 新增 **`scripts/sync-panel-config.sh`**：一次刷新**面板真正会读的每一处** —— 仓库副本、systemd 单元 `PANEL_CONFIG` 指向的安装目录、`ONECLOUD_PANEL_INSTALL_DIR` / `/opt/onecloud/panel` |
| 接入点 | `init.sh` 面板部署后自动同步；「部署 Panel」菜单新增「同步面板配置」；本机 bootstrap 结束后询问同步 |

`app.py` 的 `load_config()` **每个请求都会重新读取** `config.json`，因此配置变更通常**无需重启**面板
（`sync-panel-config.sh --restart` 可按需重启）。

### 使用
```bash
# 日常：改完 inventory/nodes.yaml（或 nodes.local.yaml）后刷新面板配置
bash scripts/sync-panel-config.sh                # 刷新所有已知位置
bash scripts/sync-panel-config.sh --restart      # 顺带重启面板服务
bash scripts/sync-panel-config.sh --dry-run      # 只显示将写入的位置
```

### 注意事项
节点清单的**最终真相在控制端**的 `inventory/`。`bootstrap.sh` 的回写只作用于**该节点本地的克隆副本**；
多机场景仍需把节点的 `nodes.local.yaml`（或对应条目）汇到控制端，再执行
`sync-panel-config.sh` 刷新面板。

### 验证
- `gen-panel-config.sh --out` 生成合法 JSON，且清单 / 环境变量里的节点 IP **透传**（`ONECLOUD_WK_EDGE_01_IP=10.77.77.77` → JSON 中即该值）；
- `sync-panel-config.sh` 在「伪 systemd 单元指向的安装目录」上真实刷新 `config.json`（旧内容被替换）；
- `update_local_inventory` 三场景：更新已有节点、追加新节点、新节点落在 `nodes:` 段内（`network:` 之前）。

---

## 4. 同类问题：远程数据根 `/mnt/sd` 硬编码

### 现象
在**无 SD 卡**的节点上，配置分发 / 备份 / 恢复 / 批量更新静默失效（操作到不存在的 `/mnt/sd/...`）。

### 根因
`bootstrap.sh` 已按 SD 卡状态自适应数据根（SD 挂载点，或回退 `/opt/onecloud`），
但控制端脚本仍一律按 `/mnt/sd` 读写远程路径 —— 写与读用两个不同的「真相」。

### 修复
1. **节点侧记录事实**：`bootstrap.sh` 写 `/etc/onecloud/install.conf`：
   ```
   NODE_NAME=... / HOSTNAME=... / NODE_IP=... / WG_IP=...
   DATA_ROOT=<实际数据根> / INSTALL_VIA_SD=0|1
   ```
2. **统一读取入口**：`lib-nodes.sh` 新增 **`node_data_root <IP|节点名>`**：
   SSH 读取节点 `install.conf` 的 `DATA_ROOT`；取不到时回退 `ONECLOUD_REMOTE_DATA_ROOT`（默认 `/mnt/sd`，兼容旧环境）。
3. **各脚本改用之**：
   | 脚本 | 变更 |
   |------|------|
   | `deploy.sh` | `REMOTE_BASE` 与 `scripts/docs/inventory` 分发路径基于实际数据根 |
   | `backup.sh` | 在 `backup_remote()` 入口把约定的 `/mnt/sd` 前缀映射到实际数据根（调用处无需逐个改） |
   | `restore.sh` | 在 `restore_to_node()` 入口做同样的映射（与备份侧同口径） |
   | `update-all.sh` | 远程 `srv` 目录来自 `REMOTE_ROOT`（经 ssh 传入远端脚本环境） |
   | `health-check.sh` | 磁盘检查改用实际数据根 |

### 验证
- `node_data_root`：SSH 不可用 → `/mnt/sd`；`ONECLOUD_REMOTE_DATA_ROOT=/opt/onecloud` → 覆盖生效；
- 路径归一化：`/mnt/sd/srv/wk-edge-01` → `/opt/onecloud/srv/wk-edge-01`（`/etc/wireguard` 等非 `/mnt/sd` 路径不受影响）；
- 五个脚本均不再硬编码远程 `/mnt/sd`（`backup.sh` / `restore.sh` 仅保留用于归一化的模式匹配）。

---

## 测试与回归

- 新增第 **32** 组测试「初始化部署修复」**40 项**，覆盖上述四个脚本/库的**静态契约**与**真实行为**。
- 全量测试见仓库根 `test_report.txt`（`python test_validate.py`）。

## 相关文件

| 文件 | 作用 |
|------|------|
| `scripts/fix-perms.sh` | 批量修复脚本执行权限（问题 1） |
| `panel/install-service.sh` | 支持 `--install-dir` 把面板装到稳定目录（问题 2） |
| `scripts/gen-panel-config.sh` | 支持 `--out`（问题 3） |
| `scripts/sync-panel-config.sh` | 把清单节点信息刷新到面板实际读取处（问题 3） |
| `scripts/lib-nodes.sh` | `node_data_root()`（问题 4） |
| `scripts/bootstrap.sh` | 回写 `nodes.local.yaml` + 写 `install.conf`（问题 3/4） |
| `init/init.sh` | 面板安装目录 + 配置同步入口 + 权限自愈（问题 1/2/3） |
