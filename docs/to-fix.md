# OneCloud 集群 — 问题清单与修复记录

> 提出：2026-09-16（项目整体验证）
> 状态：**全部 10 项已修复并复验通过**（2026-09-16 第二轮，v1.5.1）
> 复验：全量验证套件 **28 组 / 441 项，0 失败 0 警告**（耗时 17m53s）

---

## 一、修复总览

| # | 问题 | 级别 | 修法 | 状态 |
|---|---|---|---|---|
| 1 | `setup.sh` 缺 typecho / gitea / ariang 安装入口 | P1 | 补 3 个 `install_*` 并注册进 `add_service` | ✅ |
| 2 | `restore.sh` 服务白名单只 6 个 | P2 | 改用 `service_names`（读 inventory） | ✅ |
| 3 | `backup.sh` usage 承诺 `data` 无实现 | P2 | 补 `data)` 分支；类型校验提到 `mkdir` 前 | ✅ |
| 4 | 面板页脚硬编码 `v1.0` | P3 | 改为后端注入 `{{ version }}` | ✅ |
| 5 | `deploy.sh` 目录预建缺 3 项 | P3 | 补 `memos/data`、`typecho/usr`、`cups-web/config` | ✅ |
| 6 | `/api/topology` 死端点 | P3 | 删除 | ✅ |
| 7 | 空目录 + `.gitignore` 重复条目 | P3 | 删空目录；三条细粒度规则收敛 | ✅ |
| 8 | 第 11 组覆盖面不足 | P2 | 扩成**五方比对**，不一致直接 `log_fail` | ✅ |
| 9 | backup / restore 无 CLI 契约测试 | P3 | 新增第 28 组（usage ↔ case 双向比对） | ✅ |
| 10 | 文档口径未校验 | P3 | 文档里出现的子命令纳入校验；文档同步补 `data` | ✅ |

---

## 二、逐项说明

### 1. 【P1】`scripts/setup.sh` 缺 typecho / gitea / ariang 安装入口

**原状**：`add_service` 注册表 17 项，无这三个；但 `nodes.yaml`、三份 `docker-compose.yml`、
`panel/config.json`、`health-check.sh`（137/151/152 行）四处都当它们已部署。
走「统一安装」菜单装不上 → 巡检报容器未运行。

**修法**（方案 A：对齐 compose，而非标注「仅 compose」）
- 新增 `install_typecho`：`joyqi/typecho:latest`，`-p 8083:80`，卷 `typecho/usr`
- 新增 `install_gitea`：`gitea/gitea:latest`，`-p 3000:3000 -p 222:22`，卷 `gitea`，
  交互询问 `ROOT_URL`（留空用本机 IP）
- 新增 `install_ariang`：`p3terx/ariang:latest`，`-p 6880:80`，纯前端无数据卷
- 三个都加进 `add_service` 注册表（Docker 服务分组）

**为什么选 A**：README 与菜单都宣称「统一安装」覆盖节点声明的全部服务，标「仅 compose」
等于让菜单自己打自己脸；三个服务的 compose 定义已经存在，对齐成本可控。

### 2. 【P2】`scripts/restore.sh` 服务白名单只 6 个

**原状**：`KNOWN_SERVICES="homeassistant piwigo typecho aria2 syncthing gitea"`，
白名单外 `exit 1`；而 `backup.sh` 走 `node_of_service` 支持全部 18 个 →
**12 个服务备份得到、恢复不了**。

**修法**
```bash
# 服务清单统一从 inventory/services.yaml 读取 (与 backup.sh 同一口径)
KNOWN_SERVICES="$(service_names | tr '\n' ' ')"
```
顺带把节点候选从硬编码 `wk-edge-01|wk-iot-02|wk-storage-03` 改成 `node_resolve`
（支持标准名 / 短名 `edge-01` / 主机名），错误提示里的「已知节点」也改为动态输出。

### 3. 【P2】`scripts/backup.sh` usage 承诺 `data` 无实现

**原状**：usage 列了 `data  仅备份应用数据`，case 只有 all/config/node/service →
`backup.sh data` 打印 usage + `exit 1`；而且 `mkdir -p $BACKUP_DIR/$TIMESTAMP`
在 case **之前**，非法类型也会留下一个空备份目录。

**修法**
- 补 `data)` 分支：逐节点 rsync `/mnt/sd/srv/<节点>`，排除 `docker-compose.yml` 与 `.env`
- `backup_remote` 增加第 5 个可选参数传额外 rsync 参数
- 类型校验提到 `mkdir` 之前，非法类型先 `log_error` + usage + `exit 1`

### 4. 【P3】面板页脚硬编码 `v1.0`

`index.html` 改为 `OneCloud Cluster v{{ version }}`；`app.py` 的 `index()` 改成
`render_template("index.html", version=load_config().get("version", "unknown"))`。
改版本只需改一处，模板不再可能漏改。

### 5. 【P3】`deploy.sh` 远程目录预建缺 3 项

`memos/data`、`typecho/usr`、`cups-web/config` 补进 `mkdir -p` 的花括号列表。
（无功能影响，docker 会兜底；但清单散在三处各自演化，所以补了第 8 条测试来盯。）

### 6. 【P3】`/api/topology` 死端点

后端定义无任何前端 `fetch` 调用，拓扑实由 `renderTopology()` 用 `/api/status` 渲染 →
删除（该端点会把整个 config 原样返回，属于没必要的暴露面）。

### 7. 【P3】空目录 + `.gitignore` 重复条目

删除顶层空目录 `wireguard/`（git 不追踪空目录，纯残留）；
`.gitignore` 里 `wireguard/*.key` 等三条已被后面的 `wireguard/` 完全覆盖，收敛为一条
（保留注释说明忽略范围）。

### 8. 【P2】第 11 组覆盖面不足 —— 第 1 条逃检的根因

**原状**：只比对 `services.yaml` ↔ `panel/config.json`；自研 `simple_yaml_parse`；
不一致只 `log_warn`；「验证端口配置」段只有 `log_info` 无断言。

**修法**：`test_service_consistency` 重写为**五方比对**，任一处漂移 `log_fail`：

| 源 | 内容 |
|---|---|
| A `inventory/nodes.yaml` | 节点 → 服务列表 |
| B `inventory/services.yaml` | 服务 → 节点 / 是否容器 / 端口（权威） |
| C `node-*/docker-compose.yml` | compose 实际定义 |
| D `panel/config.json` | 面板展示 |
| E `scripts/setup.sh` `add_service` | 统一安装入口 |

九条断言：A⊆B、B→A 归属、容器服务→C、C⊆B、B→E、B↔D、D 归属节点、端口 B↔C、
`deploy.sh` 预建目录覆盖 compose 相对挂载。

**实现要点**（都是踩过的坑）
- 服务名归一化 `cups_web` ↔ `cups-web`，否则必误报
- compose 解析必须**限定在顶层 `services:` 段内**：顶层 `volumes:` 缩进同样是 2 空格，
  不区分会把具名卷 `cloudflared-config` 当成服务
- 含 `${...}` 的变量端口（memos）跳过静态比对 —— 值在 `.env` 里，
  `firewall-recommend.sh` 已把它单列为「需人工确认」
- `network_mode: host` 的服务不比对 `ports`

### 9. 【P3】新增第 28 组「脚本 usage 与实现一致性（CLI 契约）」

通用判据：**usage 里声明的每个枚举子命令，case 里必须有分支；反之亦然。**
这样第 2、3 条那类契约漂移会被提前抓到。九条断言：

- backup.sh usage 类型 ↔ `case "$BACKUP_TYPE"` 分支（双向）
- restore.sh usage 恢复目标 ↔ `case "$RESTORE_TARGET"` 分支
- restore.sh 不得硬编码服务白名单（须用 `service_names`）
- backup/restore 都用 `node_of_service` 定位服务（口径一致）
- **真实执行**：`backup.sh bogus` → 退出非 0 **且不产生空备份目录**
- **真实执行**：`restore.sh <id> ariang|cups-web|panel|adguard` 不得出现「无法识别」
- `index.html` 不得硬编码版本号，且后端确实注入 `version`
- 文档（README / operations.md / init/README.md）里出现的 `scripts/backup.sh <子命令>`
  必须真实存在（校验 8 处）

> 注意：同一变量常有多个 `case`（先 `--help`/校验、再主分发），
> 取分支时要**取全部出现位置的并集**，否则只能拿到第一个小 case。

### 10. 【P3】文档口径

- `README.md` 脚本表补 `data`；`docs/operations.md` 备份示例补 `config` / `data`
- `docs/operations.md` 恢复章节注明「服务名与节点名都从 inventory 解析，
  `services.yaml` 里有的服务都能按服务恢复」（原来会让人以为只有 6 个）
- 新增断言：文档里出现的 backup.sh 子命令必须在脚本里真实存在

---

## 三、复验结果

| 项目 | 结果 |
|---|---|
| 全量验证套件 | **28 组 / 441 项，0 失败 0 警告**（`EXIT=0`，17m53s） |
| 第 11 组（五方比对） | 9 项全通过（新增） |
| 第 28 组（CLI 契约） | 9 项全通过（新增） |
| 脚本语法 | 全部通过 |
| 版本五处同步 | 均为 `1.5.1` |

修复后回归到的关键行为：

- `bash scripts/backup.sh data` → 正常执行（不再打印 usage 退出）
- `bash scripts/backup.sh bogus` → 退出非 0，不产生空备份目录
- `bash scripts/restore.sh <id> ariang` → 通过服务名识别（原被白名单拒绝）
- `grep -c install_typecho scripts/setup.sh` → 2（函数定义 + 注册表；原为 0）

---

## 四、已完成但当留意的（本次确认无问题）

- 三个「孤儿服务」缺本地目录属正常：纯容器服务、无配置模板，运行时目录由部署脚本创建
- `homeassistant/configuration.yaml` 的 `!include` 是 HA 自有标签，PyYAML 报错属误报
- 面板依赖与 import 匹配；前端 5 个 fetch 端点与后端路由一一对应
- 端口一致（gitea SSH 222、ariang 6880、typecho 8083、verysync 19900、cups-web 632）
- 防火墙建议清单覆盖全部声明端口，变量端口单列人工确认

## 五、仍未覆盖（不属本次范围）

- 目录清单仍有三处来源（`deploy.sh:93` / `bootstrap.sh` / `setup.sh ensure_svc_dir`），
  本次改用**测试**盯住（第 11 组断言 deploy 覆盖 compose 挂载），没有做物理收敛
- mock 之外的真机语义：docker 行为、systemd、真实 firewalld/ufw 运行时
