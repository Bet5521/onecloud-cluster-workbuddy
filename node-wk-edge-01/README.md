# 🟢 node-wk-edge-01 — Edge Gateway 节点

边缘网关节点，承担外部访问入口、DNS 过滤、VPN 隧道和流量代理。

---

## 节点信息

| 项目 | 值 |
|------|-----|
| 主机名 | edge-01 |
| IP | 192.168.1.101 |
| WireGuard IP | 10.8.0.101 |
| 角色 | edge-gateway |

---

## 服务列表

| 服务 | 类型 | 端口 | 说明 |
|------|------|------|------|
| Cloudflare Tunnel | Docker | — | 内网穿透，无需公网 IP |
| AdGuard Home | Docker | 3000 / 53 | DNS 广告过滤 |
| WireGuard | Docker | 51820/udp | 集群 VPN 互联 |
| Memos | Docker | 5230 | 轻量笔记服务 |
| Clash (mihomo) | 原生 | 9090 | 流量代理 |

---

## 文件说明

```
node-wk-edge-01/
├── docker-compose.yml      # Docker 服务编排（cloudflared, adguard, wireguard, memos）
├── .env.example            # 环境变量模板
├── adguard/
│   └── init.sh             # AdGuard 初始化脚本
├── clash/
│   ├── config.yaml         # Clash 代理配置（需替换为订阅）
│   ├── install-binary.sh   # mihomo 安装入口（委派 scripts/install-services.sh）
│   └── install-service.sh  # systemd 服务安装入口
├── cloudflared/
│   └── config.yml          # Cloudflare Tunnel 配置
├── memos/
│   └── init.sh             # Memos 初始化脚本
└── wireguard/              # WireGuard 密钥与配置（已 gitignore，不入库）
```

---

## 快速部署

```bash
# 1. 初始化节点
./scripts/bootstrap.sh --node wk-edge-01 --yes

# 2. 分发配置
./scripts/deploy.sh -n wk-edge-01

# 3. 启动 Docker 服务
./scripts/install-services.sh edge

# 4. 安装 mihomo (原生)
./scripts/install-services.sh mihomo
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
| `CF_TUNNEL_TOKEN` | Cloudflare Tunnel Token |
| `ADGUARD_PASSWORD` | AdGuard 管理密码 |
| `CLASH_SECRET` | Clash API 密钥 |
| `MEMOS_PORT` | Memos 端口（默认 5230） |
| `DOMAIN` | 公网域名 |
