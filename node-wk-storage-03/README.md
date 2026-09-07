# 🟠 node-wk-storage-03 — Storage & Sync 节点

存储与同步节点，提供文件同步、下载、打印、代码托管等服务。

---

## 节点信息

| 项目 | 值 |
|------|-----|
| 主机名 | storage-03 |
| IP | 192.168.1.103 |
| WireGuard IP | 10.8.0.103 |
| 角色 | storage-sync |

---

## 服务列表

| 服务 | 类型 | 端口 | 说明 |
|------|------|------|------|
| Syncthing | Docker | 8384 / 22000 | 去中心化文件同步 |
| aria2 | Docker | 6800 / 6888 | 多线程下载工具 |
| AriaNg | Docker | 6880 | aria2 Web 前端 |
| CUPS | Docker | 631 | 网络打印服务 |
| CUPS Web | Docker | 632 | CUPS 管理界面 |
| Gitea | Docker | 3000 / 222 | 轻量代码托管 |
| verysync | 原生 | 19900 | 微力同步 |

---

## 文件说明

```
node-wk-storage-03/
├── docker-compose.yml      # Docker 服务编排（syncthing, aria2, ariang, cupsd, cups-web, gitea）
├── .env.example            # 环境变量模板
├── aria2/
│   ├── aria2.conf          # aria2 配置文件
│   └── init.sh             # aria2 初始化脚本
├── cupsd/
│   └── init.sh             # CUPS 初始化脚本
├── syncthing/
│   └── init.sh             # Syncthing 初始化脚本
└── verysync/
    ├── config.yaml         # verysync 配置
    ├── install.sh          # verysync 安装入口（委派 scripts/install-services.sh）
    └── install-service.sh  # systemd 服务安装入口
```

---

## 快速部署

```bash
# 1. 初始化节点
./scripts/bootstrap.sh --node wk-storage-03 --yes

# 2. 分发配置
./scripts/deploy.sh -n wk-storage-03

# 3. 启动 Docker 服务
./scripts/install-services.sh storage

# 4. 安装 verysync (原生)
# 需手动下载: https://www.verysync.com/download
```

---

## 环境变量 (.env)

从 `.env.example` 复制并填写：

```bash
cp .env.example .env
vim .env
```

关键变量：

| 变量 | 说明 |
|------|------|
| `ARIA2_RPC_SECRET` | aria2 RPC 密钥 |
| `CUPS_ADMIN_USER` | CUPS 管理员用户名 |
| `CUPS_ADMIN_PASSWORD` | CUPS 管理员密码 |
| `WG_PEER01_PUBKEY` | WireGuard Peer 01 公钥 |
| `WG_PEER02_PUBKEY` | WireGuard Peer 02 公钥 |
