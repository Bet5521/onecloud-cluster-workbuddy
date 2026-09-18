# 🖥️ panel/ — 集群控制面板

基于 Flask 的 Web 控制面板，提供集群状态监控、服务管理和快捷操作。

---

## 功能

- **节点状态监控**：在线/离线、负载、内存、磁盘
- **服务管理**：启动/停止/重启/查看日志
- **快捷操作**：全部启动/停止/拉取镜像/健康检查/备份
- **远程命令执行**：白名单安全机制，仅允许只读命令
- **集群拓扑图**：可视化展示节点关系
- **Basic Auth 认证**：保护 API 不被未授权访问
- **危险操作二次确认**：reboot/shutdown 需 confirm=true

---

## 文件说明

```
panel/
├── app.py                  # Flask 主应用
├── config.json             # 集群配置（由 scripts/gen-panel-config.sh 生成）
├── requirements.txt        # Python 依赖
├── install-service.sh      # systemd 服务安装脚本
├── static/
│   ├── css/style.css       # 样式文件
│   └── js/app.js           # 前端逻辑
└── templates/
    └── index.html          # 页面模板
```

---

## 快速启动

```bash
# 安装依赖
pip3 install -r requirements.txt

# 启动（默认端口 9000）
python3 app.py

# 自定义端口和认证
PANEL_USER=admin PANEL_PASS='强密码' PANEL_PORT=9000 python3 app.py
```

访问：`http://<edge节点IP>:9000`

### 监听地址（`PANEL_HOST`）

| 取值 | 含义 |
|------|------|
| `0.0.0.0` | 监听**全部网卡**（含 WireGuard / 外网网卡）—— 暴露面最大，需配合防火墙 |
| 本机局域网地址（如 `192.168.1.101`） | **同网段可访问**，其它网段需经路由/防火墙 |
| `127.0.0.1` | 仅本机（远端需 `ssh -L 9000:127.0.0.1:9000 <用户>@<节点IP>`） |

> 想「同网段可访问」应填**本机在该网段的地址**，不要用 `0.0.0.0` ——
> 后者会把 WireGuard 与外网网卡一起暴露出去。

`app.py` 启动前会校验 `PANEL_HOST`：网段地址（`192.168.1.0`）、广播地址
（`192.168.1.255`）、回环网段的网络地址（`127.0.0.0`）、组播/保留段、非 IPv4
字面量都会**直接报错退出并说明该怎么改**，而不是留到 `bind()` 时抛出
`Cannot assign requested address`（那条报错看不出真正原因）。

```bash
PANEL_HOST=127.0.0.0 python3 app.py
# [ERROR] PANEL_HOST=127.0.0.0 不是可用监听地址 (保留段 / 回环网段 / 组播段)
#         仅本机访问请用 127.0.0.1; 同网段访问请填本机局域网地址
```

经 `init/init.sh` 部署时，监听地址由菜单选择并走同一套校验
（`scripts/lib-panel-host.sh`），会自动探测本机可用地址作为推荐项。

---

## 安装为 systemd 服务

```bash
sudo bash install-service.sh

# 自定义监听地址/端口（不传则逐个询问, 直接回车 = 默认值）
sudo bash install-service.sh --host 192.168.1.101 --port 9000

# 监听在回环、经 SSH 转发到 19000 访问（访问端口与监听端口不同）
sudo bash install-service.sh --host 127.0.0.1 --port 9000 --url-port 19000

# 等价的环境变量写法 + 全程不询问（自动化场景）
sudo env PANEL_HOST=192.168.1.101 PANEL_PORT=9000 bash install-service.sh -y
```

| 参数 | 含义 | 默认 |
|------|------|------|
| `--host` / `PANEL_HOST` | **监听地址**（绑哪张网卡） | `0.0.0.0` |
| `--port` / `PANEL_PORT` | **监听端口** | `9000` |
| `--url-host` / `PANEL_URL_HOST` | **访问地址**：面板 IP 或域名 | 自动探测本机地址 |
| `--url-port` / `PANEL_URL_PORT` | **访问端口**（反代 / 端口映射 / `ssh -L` 时与监听端口不同） | 同监听端口 |
| `-y` / `--yes` | 全部取环境变量/默认值, 不询问 | — |

三者的区别：**监听**决定进程 bind 到哪里（必须是本机网卡地址或 `0.0.0.0`）；
**访问**只是浏览器里敲的入口，Nginx 反代、路由器端口映射、SSH 端口转发都会让
它与监听值不一致，所以单独可配。

`install-service.sh` 会在写 unit 之前**再校验一次** `PANEL_HOST`：即使绕过
`init.sh` 直接调用，也不会把绑不上的地址写进服务配置（否则只会得到一个起不来的
服务，`systemctl status` 里只有一行 `Cannot assign requested address`）。
网段地址（`192.168.1.0`）、回环网段的网络地址（`127.0.0.0`）、组播/保留段、
非法端口都会被当场拒绝并给出替代值。启动后会按监听类型打印真实入口
（`0.0.0.0` 列出各网卡地址、`127.0.0.1` 提示仅本机并给出 `ssh -L` 命令）。

参数同时写入 `/etc/onecloud/panel.env`（权限 600，**合并**写入 —— 只更新
`PANEL_HOST` / `PANEL_PORT` / `PANEL_URL_HOST` / `PANEL_URL_PORT` 四个键，
不会冲掉 `init.sh` 放的账号密码），systemd unit 通过 `EnvironmentFile` 引用它。

---

## API 接口

所有 API 需要 Basic Auth 认证。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 控制面板页面 |
| GET | `/api/status` | 获取集群状态 |
| GET | `/api/topology` | 获取拓扑配置 |
| POST | `/api/node/<name>/action` | 节点操作（reboot/shutdown/docker_*） |
| POST | `/api/service/<node>/<svc>/<action>` | 服务操作（start/stop/restart/logs） |
| POST | `/api/exec` | 远程执行命令（白名单） |

### 安全限制

**命令白名单**：仅允许 `free`、`df`、`ls`、`docker ps`、`uptime`、`systemctl status` 等只读命令。

**拦截规则**：
- Shell 元字符：`;` `&&` `||` `|` `` ` `` `$(` 等
- 危险命令：`rm -rf`、`mkfs`、`dd if=`、`shutdown`、`reboot` 等

---

## 配置文件

`config.json` 由 `scripts/gen-panel-config.sh` 从 `inventory/nodes.yaml` 自动生成：

```json
{
  "cluster_name": "OneCloud Cluster",
  "version": "1.6.0",
  "nodes": [
    {
      "name": "wk-edge-01",
      "display_name": "Edge Gateway",
      "ip": "192.168.1.101",
      "wg_ip": "10.8.0.101",
      "color": "#4CAF50",
      "services": [...]
    }
  ]
}
```

IP 变更后重新生成：

```bash
./scripts/gen-panel-config.sh
```
