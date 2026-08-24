# 🛠️ OneCloud 集群部署与运维指南

> 基于玩客云 WS1608 (Amlogic S805, ARMv7, 1GB RAM) 多节点组建的家庭服务集群

---

## 📋 项目概述

### 硬件规格

| 项目 | 规格 |
|------|------|
| CPU | Amlogic S805 四核 1.5GHz |
| 架构 | ARMv7 (32-bit) |
| 内存 | 1GB DDR3（每个节点挂载 2GB swap） |
| 存储 | 8GB eMMC（系统）+ 128GB SD 卡（Docker 数据） |
| 网络 | 100Mbps Ethernet |

### 节点角色划分

| 节点 | 主机名 | IP | 角色 | 服务 |
|------|--------|----|------|------|
| NODE-01 | wk-edge-01 | 192.168.1.101 | Edge Gateway | cloudflared, AdGuard Home, WireGuard, Memos, mihomo(Clash), Panel |
| NODE-02 | wk-iot-02 | 192.168.1.102 | IoT Core | Home Assistant, Piwigo, xiaomusic, migpt, Typecho |
| NODE-03 | wk-storage-03 | 192.168.1.103 | Storage & Sync | Syncthing, aria2/AriaNg, CUPS/cups-web, verysync, Gitea |

### 网络架构

```
外部网络
    │
    ▼
Cloudflare Zero Trust (零信任访问)
    │
    ▼
wk-edge-01 (192.168.1.101)
  ├── AdGuard Home (家庭 DNS)
  ├── WireGuard Server (集群互联)
  └── Clash (流量代理)
    │
    ▼ WireGuard Mesh (10.8.0.0/24)
    ├── wk-iot-02 (10.8.0.102)
    └── wk-storage-03 (10.8.0.103)
```

### 服务端口矩阵

| 服务 | 节点 | 端口 | 协议 | 公网访问 |
|------|------|------|------|---------|
| AdGuard Home | edge-01 | 3000/53 | TCP/UDP | ✅ CF Tunnel |
| WireGuard | edge-01 | 51820 | UDP | ✅ CF Tunnel |
| Memos | edge-01 | 5230 | TCP | ✅ CF Tunnel |
| Clash Web | edge-01 | 9090 | TCP | ❌ 内网 |
| Cluster Panel | edge-01 | 9000 | TCP | ✅ CF Tunnel |
| Home Assistant | iot-02 | 8123 | TCP | ✅ CF Tunnel |
| Piwigo | iot-02 | 8080 | TCP | ✅ CF Tunnel |
| Typecho | iot-02 | 8083 | TCP | ✅ CF Tunnel |
| xiaomusic | iot-02 | 8081 | TCP | ❌ 内网 |
| migpt | iot-02 | 8082 | TCP | ❌ 内网 |
| Syncthing | storage-03 | 8384/22000 | TCP | ❌ 内网 |
| aria2 | storage-03 | 6800 | TCP | ❌ 内网 |
| AriaNg | storage-03 | 6880 | TCP | ❌ 内网 |
| CUPS | storage-03 | 631 | TCP | ❌ 内网 |
| CUPS Web | storage-03 | 632 | TCP | ❌ 内网 |
| Gitea | storage-03 | 3000 | TCP | ✅ CF Tunnel |
| Gitea SSH | storage-03 | 222 | TCP | ❌ 内网 |
| verysync | storage-03 | 19900 | TCP | ❌ 内网 |

---

## 📁 目录结构

```
onecloud-cluster/
├── README.md
├── docs/
│   ├── requirements.md
│   ├── architecture.md
│   ├── topology.md
│   ├── operations.md
│   └── cloudflare-setup.md
├── inventory/
│   ├── nodes.yaml
│   └── services.yaml
├── node-wk-edge-01/      # Edge Gateway
├── node-wk-iot-02/      # IoT Core
├── node-wk-storage-03/  # Storage & Sync
├── panel/               # Flask 控制面板
└── scripts/             # 运维脚本
```

---

## 🚀 快速开始

### 1. 初始化节点

```bash
./scripts/bootstrap.sh --node wk-edge-01    --ip 192.168.1.101
./scripts/bootstrap.sh --node wk-iot-02     --ip 192.168.1.102
./scripts/bootstrap.sh --node wk-storage-03 --ip 192.168.1.103
```

### 2. 生成 WireGuard 密钥

```bash
./scripts/wireguard-setup.sh
```

### 3. 分发配置并启动

```bash
./scripts/deploy.sh
./scripts/deploy.sh --exec "docker-compose up -d"
```

### 4. 启动控制面板

```bash
cd panel && pip3 install -r requirements.txt && python3 app.py
# 访问 http://192.168.1.101:9000
```

---

## 📚 详细文档

| 文档 | 内容 |
|------|------|
| [docs/requirements.md](docs/requirements.md) | 需求分析、功能清单 |
| [docs/architecture.md](docs/architecture.md) | 架构设计、网络拓扑、存储规划 |
| [docs/topology.md](docs/topology.md) | 可视化拓扑图 (Mermaid + ASCII) |
| [docs/operations.md](docs/operations.md) | 运维手册 |
| [docs/cloudflare-setup.md](docs/cloudflare-setup.md) | Cloudflare Tunnel 配置 |

---

## 📝 License

MIT License
