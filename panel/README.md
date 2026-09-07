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

---

## 安装为 systemd 服务

```bash
sudo bash install-service.sh
```

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
  "version": "1.2.0",
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
