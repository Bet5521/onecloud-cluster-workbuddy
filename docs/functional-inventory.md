# 功能清单与说明（Functional Inventory）

> 版本：v1.5.5
> 适用范围：OneCloud WS1608 三节点自托管集群（Amlogic S805 / ARMv7 / 1GB RAM）
> 本文件的目的：把项目「有什么、在哪、怎么用、怎么验证」一次说清。所有条目均可回溯到仓库内文件。

---

## 1. 项目定位

在一台（或多台）玩客云 WS1608 上，用 **Docker Compose + 原生 systemd 服务** 搭出一套自托管集群，
并通过一个 **Web 面板** 做统一状态查看与操作，通过 **WireGuard** 或 **纯局域网** 组网，
通过 **Cloudflare Tunnel** 对外暴露少数服务。

设计原则（决定了后面所有实现细节）：

| 原则 | 落地方式 |
| --- | --- |
| 单一数据源 | 节点/服务的唯一真相在 `inventory/nodes.yaml` + `inventory/services.yaml`，其余全部派生 |
| 禁止硬编码 IP | 所有脚本通过 `scripts/lib-nodes.sh` 取值，环境变量 > `nodes.local.yaml` > 清单 |
| 数据根自适应 | 有可用 SD 卡 → 用 SD 挂载点；无卡 → 回退 `/opt/onecloud`（`scripts/lib-install-path.sh`） |
| 部署侧零防火墙改动 | 部署脚本不写 iptables；改为生成建议清单，由用户手动执行独立仓库的 `setup_firewall.sh` |
| 生成物不入库 | `wg0.conf`（含私钥）、`.env`、`panel/config.json` 之外的运行态目录均 gitignore |

---

## 2. 节点清单

来源：`inventory/nodes.yaml`（可通过 `inventory/nodes.local.yaml` 或环境变量覆盖，后者不入库）

| 节点名 | 简称 | 角色 | LAN IP | WireGuard IP | 主机名 | 定位 |
| --- | --- | --- | --- | --- | --- | --- |
| `wk-edge-01` | `edge-01` | `edge-gateway` | 192.168.1.101 | 10.8.0.101 | `edge-01.lan` | 边缘网关（WireGuard Hub / 隧道 / DNS 过滤 / 面板） |
| `wk-iot-02` | `iot-02` | `iot-core` | 192.168.1.102 | 10.8.0.102 | `iot-02.lan` | 智能家居 / 媒体 |
| `wk-storage-03` | `storage-03` | `storage-sync` | 192.168.1.103 | 10.8.0.103 | `storage-03.lan` | 存储 / 同步 / Git / 打印 |

`network:` 段（同一文件）：

| 字段 | 值 | 用途 |
| --- | --- | --- |
| `lan_subnet` | `192.168.1.0/24` | 静态 IP 写入的风险预检、防火墙 LAN 放行 |
| `wg_subnet` | `10.8.0.0/24` | WireGuard 网段；同时作为 `ALLOWEDIPS` 与健康检查依据 |
| `gateway` | `192.168.1.1` | 静态路由默认网关 |
| `dns` | `dhcp` | 默认不写 `nameserver`，沿用 DHCP 下发 |
| `wg_port` | `51820` | Hub 监听端口（UDP） |
| `domain` | `yourdomain.com` | Cloudflare Tunnel 与 `SERVERURL` |

**取值优先级**（`scripts/lib-nodes.sh`）：

```
环境变量 ONECLOUD_<节点大写>_<字段>   >   inventory/nodes.local.yaml   >   inventory/nodes.yaml
```

`inventory/nodes.local.yaml` 由 `bootstrap.sh` 结尾自动回写（幂等 upsert），因此 **控制端 `inventory/` 始终是最终真相**。

---

## 3. 服务清单

来源：`inventory/services.yaml`（18 个服务）。`container: true` = Docker 容器；`container: false` = 节点原生 systemd 服务。

### 3.1 按节点分布

| 节点 | 容器服务 | 原生服务 | 合计 |
| --- | --- | --- | --- |
| `wk-edge-01` | `cloudflared` `adguard` `wireguard` `memos` | `clash` `panel` | 6 |
| `wk-iot-02` | `homeassistant` `piwigo` `typecho` | `xiaomusic` `migpt` | 5 |
| `wk-storage-03` | `syncthing` `aria2` `ariang` `cupsd` `cups-web` `gitea` | `verysync` | 7 |
| — | — | — | **18** |

### 3.2 按功能分类（对应 `docs/requirements.md` §3 的五类需求）

#### ① 文件管理 / 同步

| 服务 | 类型 | 节点 | 端口 | 镜像 / 二进制 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `syncthing` | 容器 | storage-03 | 8384/tcp, 22000/tcp+udp, 21027/udp | `syncthing/syncthing:latest` | 双向文件同步 |
| `verysync` | 原生 | storage-03 | 19900 | 官方 armv7 二进制 | 微力同步（**需手动下载，见 §7-3**） |
| `aria2` | 容器 | storage-03 | 6800/tcp, 6888/tcp+udp | `p3terx/aria2-pro:latest` | 下载器（RPC + BT） |
| `ariang` | 容器 | storage-03 | 6880→80 | `p3terx/ariang:latest` | aria2 的 Web 前端 |
| `piwigo` | 容器 | iot-02 | 8080→80 | `linuxserver/piwigo:latest` | 照片相册 |
| `gitea` | 容器 | storage-03 | 3000/tcp, 222→22 | `gitea/gitea:latest` | 自建 Git 服务 |

#### ② 智能家居 / 媒体

| 服务 | 类型 | 节点 | 端口 | 镜像 / 二进制 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `homeassistant` | 容器 | iot-02 | host 网络 | `${HA_IMAGE}`（默认 `ghcr.io/adyoull/ha-armv7:latest`） | 智能家居中枢，`privileged` |
| `xiaomusic` | 原生 | iot-02 | 8081 | Python + `xiaomusic` | 小爱音箱音乐助手 |
| `migpt` | 原生 | iot-02 | 8082 | Python + `migpt` | 小爱同学 GPT 桥接 |

#### ③ 网络与安全

| 服务 | 类型 | 节点 | 端口 | 镜像 / 二进制 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `wireguard` | 容器 | edge-01 | 51820/udp | `linuxserver/wireguard:latest` | 异地组网 Hub，`PEERS=3`，`ALLOWEDIPS=10.8.0.0/24,192.168.1.0/24` |
| `clash` | 原生 | edge-01 | 9090 | `mihomo-linux-armv7` → `/usr/local/bin/mihomo` | 透明代理，配置 `./clash/config.yaml` |
| `adguard` | 容器 | edge-01 | host 网络（53/80/3000） | `adguard/adguardhome:latest` | DNS 过滤 |
| `cloudflared` | 容器 | edge-01 | 无入站 | `cloudflare/cloudflared:latest` | 出站隧道 `tunnel --no-autoupdate run` |

#### ④ 生产力 / 知识

| 服务 | 类型 | 节点 | 端口 | 镜像 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `memos` | 容器 | edge-01 | `${MEMOS_PORT}` | `neosmemo/memos:stable` | 轻量笔记 |
| `typecho` | 容器 | iot-02 | 8083→80 | `joyqi/typecho:latest` | 博客 |

#### ⑤ 集群管理与打印

| 服务 | 类型 | 节点 | 端口 | 镜像 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `panel` | 原生 | edge-01 | 9000 | Flask（`panel/app.py`） | 集群 Web 面板 |
| `cupsd` | 容器 | storage-03 | 631 | `ousia/cupsd:armhf` | 打印服务 |
| `cups-web` | 容器 | storage-03 | 632→80 | `nkn-ts/cups-web:armhf` | 打印 Web 界面 |

### 3.3 对外暴露（`public_access`）

| 服务 | 类型 | 公开主机名 | 回源 |
| --- | --- | --- | --- |
| `cloudflared` | cloudflare-tunnel | `tunnel.yourdomain.com` | `http://edge-01.lan:9000` |
| `adguard` | cloudflare-tunnel | `dns.yourdomain.com` | `http://edge-01.lan:3000` |

其余服务不出公网，仅 LAN / WireGuard 内可达。

---

## 4. 脚本清单

`scripts/` 共 24 个文件：4 个库（`lib-*`）+ 20 个可执行脚本。

### 4.1 库（被 source，不单独执行）

| 文件 | 行数级别 | 职责 | 关键接口 |
| --- | --- | --- | --- |
| `lib-nodes.sh` | ~400 | **唯一节点/服务清单加载器**；awk 手写 YAML 解析；数据根探测 | `load_nodes` `node_ip` `node_wg_ip` `node_resolve` `node_by_ip` `node_name_by_role` `node_of_service` `node_data_root` `require_nodes` |
| `lib-install-path.sh` | ~398 | **安装路径决策引擎**：设备→分区→挂载态→可写→空间，逐级判定，失败降级 `/opt/onecloud` | `sd_probe` `sd_evaluate` `resolve_data_root` `install_path_for` `safe_install_dir/tree/file` |
| `lib-pydeps.sh` | ~247 | **Python 依赖四级降级链**：pip → `--break-system-packages` → ensurepip/apt → 发行版包 | `ensure_pip` `verify` `install` `install_from_file` `hint` |
| `lib-panel-host.sh` | — | **面板监听地址校验**（三层共用：`init.sh` / `install-service.sh` / `app.py`） | `validate_bind_host` `resolve_bind_host` |
| `lib-network-audit.sh` | — | **只读**通路自检：探测不到（非 root / 无 iptables·nft）一律 `unknown`，不当风险 | 供 `health-check.sh` / `init.sh` 调用 |

> `lib-install-path.sh` 的判定顺序：设备存在 → 分区存在 → **运行时**查挂载点（`findmnt`/`mountpoint`，**不写死 `/mnt/sd`**）→ 可读写 → 剩余空间 ≥ `SD_MIN_SPACE_MB`（默认 512）。全满足才用 SD，否则回退 `/opt/onecloud`，且不阻断流程。

### 4.2 可执行脚本

| 脚本 | 用途 | 常用参数 |
| --- | --- | --- |
| `init/init.sh` | **推荐入口**：纯交互菜单，编排下列所有脚本 | 无参数（传入即 `exit 2`） |
| `bootstrap.sh` | 节点初始化：探测 / 装包 / 静态 IP / SD 卡 / 数据根 / 写 `install.conf` / 回写 `nodes.local.yaml` | `--dry-run` `--no-sd` `--mirror` `--apt-update` `--apt-upgrade` `--no-apt-pkgs` `--dns dhcp\|auto\|none` `--with-wg-firewall` `--extra-pkgs` |
| `setup.sh` | 节点侧交互式安装器（服务 / 依赖 / 端口自检） | 交互 |
| `deploy.sh` | 从控制端把 compose/配置分发到节点 | `-n <节点>` `-t` `--dry-run` |
| `wireguard-setup.sh` | 生成 `wg0.conf` 与密钥、`add peer` / `list` / `gen` | `--with-wg-firewall` / `--no-wg-firewall` |
| `install-services.sh` | 原生（非容器）服务安装：`clash` `xiaomusic` `migpt` `verysync` `panel` | `<服务名>` / `edge\|iot\|storage\|all-docker` |
| `gen-node-env.sh` | 由清单渲染各节点 `.env`（**保留用户密钥**） | — |
| `gen-panel-config.sh` | 由清单渲染 `panel/config.json` | `--out FILE_OR_DIR` |
| `sync-panel-config.sh` | 把 `config.json` 刷新到面板**真正读取的每一处**（仓库副本 / systemd 单元指向目录 / `/opt/onecloud/panel`） | `--restart` |
| `health-check.sh` | 集群健康检查（容器 / 端口 / WireGuard / 磁盘） | — |
| `backup.sh` | 备份配置与数据 | `all` / `config` / `data` / `node <名>` / `service <名>` |
| `restore.sh` | 从备份恢复 | `<备份ID> <目标>` / `<备份ID> service <名>` |
| `update-all.sh` | 批量更新镜像 / 系统包（**不升 Docker Engine**） | `-d` / `-s` / `-a` / `-n <节点>` |
| `firewall-recommend.sh` | **纯静态**生成防火墙建议清单（不做任何 iptables 调用） | `--emit-dsl` `--stdout` `--out` `--lan` |
| `sd-format.sh` | SD 卡格式化为 ext4（拦截根盘） | `--dry-run` |
| `sd-migrate.sh` | eMMC → SD 迁移（rsync + 改写 Docker data-root，默认保留来源） | `--dry-run` |
| `sd-replace.sh` | SD → USB 打包 + SHA256 | `--dry-run` |
| `sd-tools.sh` | 上述三个的菜单 / 非交互透传入口 | `--dry-run` |
| `fix-perms.sh` | 修复 `.sh` 执行权限（幂等） | `--list` `--dry-run` `--with-py` `--root` |

**编排关系**（`init/init.sh` → 其余）：

```
init.sh
├─ menu_panel         ─→ panel/install-service.sh（--host/--port/--url-host/--url-port）→ sync-panel-config.sh
├─ menu_deploy_node   ─→ bootstrap.sh / deploy.sh
├─ menu_wireguard     ─→ wireguard-setup.sh
├─ menu_services      ─→ setup.sh / gen-node-env.sh / deploy.sh
├─ menu_native_services → install-services.sh
├─ menu_maintenance   ─→ health-check.sh / backup.sh / restore.sh / update-all.sh / sd-tools.sh
├─ menu_selfcheck     ─→ lib-network-audit.sh + firewall-recommend.sh
└─ 预检自愈           ─→ fix-perms.sh
```

---

## 5. 面板功能

| 层 | 文件 | 功能 |
| --- | --- | --- |
| 后端 | `panel/app.py`（306 行） | Flask；每请求重读配置；`Basic-Auth` 网关；`/api/exec` 命令白名单 |
| 模板 | `panel/templates/index.html`（74 行） | 单页；节点卡片 / 服务列表 / 拓扑区 / 页脚版本号 |
| 前端 | `panel/static/js/app.js`（265 行） | 定时刷新、服务/节点/集群操作、命令执行 |
| 样式 | `panel/static/css/style.css` | 深色主题 |
| 配置 | `panel/config.json`（41 行） | 由 `gen-panel-config.sh` 生成 |

**API 一览**：

| 方法 | 路径 | 鉴权 | 作用 |
| --- | --- | --- | --- |
| GET | `/` | 否 | 页面 |
| GET | `/api/status` | **是** | 全集群状态（节点在线、服务运行、磁盘） |
| POST | `/api/node/<name>/action` | **是** | `reboot` / `shutdown` 等 |
| POST | `/api/service/<node>/<svc>/<action>` | **是** | `start` / `stop` / `restart` |
| POST | `/api/exec` | **是** | 受限命令（白名单校验） |

**监听地址三层共用 `lib-panel-host.sh`**（`init.sh` → `install-service.sh` → `app.py:resolve_bind_host`），拦截网段地址 / 广播 / `127.0.0.0/8` 非 `.1` / 组播 / 保留 / 链路本地 / `0/8` / 非 IPv4。
「同网段可访问」= 绑本机在该网段的**具体地址**，不是 `0.0.0.0`。

---

## 6. 组网与数据流

```
                    ┌──────────── Cloudflare Edge ────────────┐
                    │  tunnel.yourdomain.com (panel)          │
                    │  dns.yourdomain.com    (adguard)        │
                    └───────────────┬─────────────────────────┘
                         出站隧道   │
┌───────────────────────────────────┴────────────────────────────────┐
│  wk-edge-01  192.168.1.101 / 10.8.0.101   [edge-gateway]           │
│    wireguard(Hub,51820/udp)  adguard  cloudflared  memos  clash  panel │
└───────────┬────────────────────────────────┬───────────────────────┘
            │  WireGuard 10.8.0.0/24          │  LAN 192.168.1.0/24
┌───────────┴──────────────┐    ┌────────────┴──────────────────────┐
│ wk-iot-02  .102 / .102   │    │ wk-storage-03  .103 / .103        │
│  [iot-core]              │    │  [storage-sync]                   │
│  homeassistant xiaomusic │    │  syncthing verysync aria2 ariang  │
│  migpt piwigo typecho    │    │  gitea cupsd cups-web             │
└──────────────────────────┘    └───────────────────────────────────┘
```

- **数据根**：`bootstrap.sh` 按 SD 卡状态自适应决定，并写入节点 `/etc/onecloud/install.conf`
  （`NODE_NAME` / `HOSTNAME` / `NODE_IP` / `WG_IP` / `DATA_ROOT` / `INSTALL_VIA_SD`）。
  远端读取统一走 `lib-nodes.sh: node_data_root <IP|节点名>`（SSH 读 `install.conf`，取不到回退
  `ONECLOUD_REMOTE_DATA_ROOT` 或 `/mnt/sd`）。`deploy` / `backup` / `restore` / `update-all` / `health-check` **必须**用它。
- **部署布局**：控制端 `deploy.sh` 计算 `REMOTE_ROOT="$(node_data_root "$NODE_IP")"`，
  `REMOTE_BASE="${REMOTE_ROOT}/srv/${NODE_NAME}"`；`bootstrap.sh` 预先建好 `srv/<节点名>/<服务>/...` 目录树。

---

## 7. 已知功能缺口（清单层面）

这三项在「清单 / 面板 / 实际」之间存在落差，**不影响其他服务的正常使用**，详见 `docs/audit-2026-09.md`：

1. **`verysync`**：`services.yaml` 与 `panel/config.json` 都登记了它（storage-03，原生，19900），
   但 `docker-compose.yml` 里没有（它是原生服务，这点正常），而 `install-services.sh: install_verysync()`
   只打印「需要从官网手动下载」——**没有任何自动化安装路径**，`health-check.sh` 也不检查它。
2. **`clash` / `panel`**：声明为原生，正确地从 edge compose 中缺席；但 `clash` 的配置目录
   指向 `/mnt/sd/edge-01/clash`（**无 `srv/` 段**），与部署布局 `<DATA_ROOT>/srv/<节点名>` 不一致。
3. **无「已安装 / 未安装」模型**：`services.yaml` 没有 `optional` / `enabled` 字段；
   `gen-panel-config.sh` 无条件把清单里的服务全部写进面板；`app.py` 只探测 `running`，不报 `installed`。

---

## 8. 验证与自检

| 入口 | 覆盖 | 结果（v1.6.0） |
| --- | --- | --- |
| `python test_validate.py` | 36 组 | 全通过（报告写入 `test_report.txt`，已 gitignore） |
| `scripts/lib-network-audit.sh` | 网络通路 / 防火墙 / SSH 通道 | 只读；探测不到一律 `unknown` |
| `scripts/health-check.sh` | 容器 / 端口 / WireGuard / 磁盘 | 运行态检查 |
| `scripts/fix-perms.sh --list` | `.sh` 执行权限 | 幂等修复 |

**36 组测试分布**（`test_validate.py` 中的 `def test_*`）：

| # | 组名 | 关注点 |
| --- | --- | --- |
| 1 | `test_config_files` | YAML / JSON 配置完整性 |
| 2 | `test_node_directories` | 节点目录结构 |
| 3 | `test_script_syntax` | 全部 `.sh` 语法 |
| 4 | `test_deploy_node_mapping` | `deploy.sh` 节点目录映射 |
| 5 | `test_fallback_mechanism` | `restore.sh` / `backup.sh` 回退 |
| 6 | `test_health_check_variables` | `health-check.sh` 变量声明位置 |
| 7 | `test_cloudflared_command` | `setup.sh` cloudflared 命令 |
| 8 | `test_panel_app_error_handling` | `panel/app.py` 错误处理 |
| 9 | `test_xiaomusic_download` | `install-services.sh` xiaomusic 下载 |
| 10 | `test_panel_install_service` | `panel/install-service.sh` 路径变量化 |
| 11 | `test_service_consistency` | **服务清单五方一致性**（清单 / compose / panel / setup.sh 注册表） |
| 12 | `test_cli_contract` | README / 运维手册记载的 CLI 确实被实现 |
| 13 | `test_ip_customizable` | IP 全部来自清单、无硬编码旧 IP |
| 14 | `test_safety_regression` | 已修缺陷不得复现 |
| 15 | `test_panel_frontend_contract` | 前端调用的 API 真实存在且方法/参数匹配 |
| 16 | `test_bootstrap_network_resolution` | 网络取值（IP/网关联动 + 本机探测） |
| 17 | `test_bootstrap_sd_and_risk` | 启动探测 / SD 决策 / 写 IP 前风险预检 |
| 18 | `test_init_entrypoint` | 交互式入口契约（无参数 / 菜单齐全 / 引用脚本存在） |
| 19 | `test_delivery_consistency` | 版本声明一致 / 行尾防护 / 表格排版 |
| 20 | `test_pydeps_fallback` | Python 依赖降级链 |
| 21 | `test_bootstrap_apt_sources` | apt 源按系统代号渲染 / 失败诊断 / dkms 条件装 |
| 22 | `test_bootstrap_dns_mode` | DNS「DHCP 自动获取」默认 |
| 23 | `test_panel_listen_host` | 面板监听地址校验 |
| 24 | `test_bootstrap_apt_optional` | 换源/更新可选且默认跳过 |
| 25 | `test_network_audit` | 网络通路 / 防火墙 / SSH 通道自检 |
| 26 | `test_panel_install_params` | 面板监听地址与访问地址分离 |
| 27 | `test_deploy_no_firewall` | 部署零防火墙写入 + 建议清单生成 |
| 28 | `test_cli_usage_contract` | usage 声明的能力必须真的实现 |
| 29 | `test_bootstrap_pkg_slim` | 装包三档制（核心 7 / 可选 11 / GUI 黑名单 45） |
| 30 | `test_install_path_adaptive` | SD 卡 → `/opt/onecloud` 回退 |
| 31 | `test_sd_tools` | SD 卡工具箱（格式化 / 迁移 / 更换） |
| 32 | `test_init_deploy_sync` | 权限 / 迁移 / IP 同步 / 数据根一致性 |
| 33 | `test_lib_services` | `lib-services.sh` 安装态 / 模式 / 数据根单一真相 |
| 34 | `test_optional_components_chain` | 可选组件全链路（7 条反向断言） |
| 35 | `test_network_modes_and_panel` | 三种组网模式 / 面板三态 / 版本同步 |
| 36 | `test_services_lib_perf_and_ports` | 性能回归（禁逐节点 SSH）+ 容器端口解析 |

> 跑测注意：必须在 PATH 含 Git `bin` + `usr/bin` 的 shell 里执行，否则 harness 找不到 `bash` → 大量
> `[WinError 2]` 假失败。Windows 一轮 20–30 分钟，可用 `ONECLOUD_TEST_HARNESS_TIMEOUT` 调整上限。
> 改完 `test_validate.py` **必须重开一轮** —— Python 启动时已把文件读进内存，后台跑测期间改文件对当轮无效。

---

## 9. 文档索引

| 文件 | 内容 |
| --- | --- |
| `README.md` | 项目总览、快速开始、脚本表、验证、变更说明 |
| `docs/requirements.md` | 背景、硬件/网络、功能需求（5 类）、非功能需求、约束、交付物 |
| `docs/architecture.md` | 分层架构与模块职责 |
| `docs/topology.md` | 物理/逻辑拓扑 |
| `docs/operations.md` | 日常运维手册 |
| `docs/cloudflare-setup.md` | 隧道与公网暴露配置 |
| `docs/package-trim.md` | 初始化装包三档制说明 |
| `docs/install-path.md` | 安装路径自适应决策 |
| `docs/sd-tools.md` | SD 卡工具箱 |
| `docs/init-deploy-fixes.md` | v1.5.5 初始化/部署四处一致性修复 |
| `docs/to-fix.md` | 问题清单与修复记录 |
| `docs/firewall/<节点>.txt` | 生成的防火墙建议清单（DSL 行 + Hub 转发命令段） |
| `docs/functional-inventory.md` | **本文件**：功能清单与说明 |
| `docs/audit-2026-09.md` | 代码库质量与功能验证报告 |
| `scripts/README.md` | 脚本级详细说明 |
| `panel/README.md` | 面板部署与配置 |
| `node-wk-*/README.md` | 各节点说明 |
