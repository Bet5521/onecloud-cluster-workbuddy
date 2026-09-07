# 架构设计文档

## 1. 总体架构

采用**单节点 Docker Compose + 节点间 WireGuard Mesh** 的集群架构。不使用 Kubernetes/K3s 等重型编排系统，以避免 1GB 内存设备上的资源浪费。

### 1.1 架构原则
1. **功能域隔离**：每节点承担一类主要功能
2. **最小化共享**：避免跨节点服务依赖
3. **零侵入节点**：通过 rsync 分发配置，不引入额外依赖
4. **可独立运行**：任一节点宕机不影响其他节点服务

## 2. 节点架构

### 2.1 NODE-01: Edge Gateway (192.168.1.101)
- LAN: 192.168.1.101 / WG: 10.8.0.101
- Docker: cloudflared, AdGuard Home, WireGuard, Memos
- 原生: mihomo (Clash), Flask Panel

### 2.2 NODE-02: IoT Core (192.168.1.102)
- LAN: 192.168.1.102 / WG: 10.8.0.102
- Docker: Home Assistant (host network), Piwigo, Typecho
- 原生: xiaomusic, migpt

### 2.3 NODE-03: Storage & Sync (192.168.1.103)
- LAN: 192.168.1.103 / WG: 10.8.0.103
- Docker: Syncthing, aria2-pro, CUPS, CUPS Web, Gitea
- 原生: verysync

## 3. 网络架构

### 3.1 WireGuard Mesh 网络
```
wg_subnet: 10.8.0.0/24
NODE-01 wg0: 10.8.0.101/32
NODE-02 wg0: 10.8.0.102/32
NODE-03 wg0: 10.8.0.103/32
```

### 3.2 Cloudflare Tunnel 路由
| Public Hostname | Service URL |
|---|---|
| panel.yourdomain.com | http://edge-01.lan:9000 |
| dns.yourdomain.com | http://edge-01.lan:3000 |
| memos.yourdomain.com | http://edge-01.lan:5230 |
| home.yourdomain.com | http://iot-02.lan:8123 |
| photos.yourdomain.com | http://iot-02.lan:8080 |
| blog.yourdomain.com | http://iot-02.lan:8083 |
| git.yourdomain.com | http://storage-03.lan:3000 |

### 3.3 端口分配总表
| 端口 | 协议 | 服务 | 节点 |
|---|---|---|---|
| 53 | TCP/UDP | AdGuard Home DNS | edge-01 |
| 3000 | TCP | AdGuard Home WebUI | edge-01 |
| 51820 | UDP | WireGuard | edge-01 |
| 5230 | TCP | Memos | edge-01 |
| 9000 | TCP | 集群控制面板 | edge-01 |
| 9090 | TCP | Clash API | edge-01 |
| 8123 | TCP | Home Assistant | iot-02 |
| 8080 | TCP | Piwigo | iot-02 |
| 8083 | TCP | Typecho | iot-02 |
| 8081 | TCP | xiaomusic | iot-02 |
| 8082 | TCP | migpt | iot-02 |
| 631 | TCP | CUPS | storage-03 |
| 6800 | TCP | aria2 RPC | storage-03 |
| 8384 | TCP | Syncthing WebUI | storage-03 |
| 22000 | TCP/UDP | Syncthing | storage-03 |
| 3000 | TCP | Gitea Web | storage-03 |
| 222 | TCP | Gitea SSH | storage-03 |

## 4. 存储架构

### 4.1 挂载方案
```
/dev/mmcblk1p1 (128GB, ext4) → 挂载到 /mnt/sd
Docker 数据迁到 /mnt/sd/docker
应用数据放 /mnt/sd/srv/<node>/
```

### 4.2 目录组织
```
/mnt/sd/
├── docker/                    # Docker 数据目录
├── srv/                       # 应用数据 (目录名与节点名一致)
│   ├── wk-edge-01/
│   ├── wk-iot-02/
│   └── wk-storage-03/
├── backups/
└── logs/
```

## 5. 内存分配策略

### 5.1 每节点 swap 配置 (2GB)
```bash
fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

### 5.2 内存分配表
| 节点 | 系统 | 服务 | Swap |
|---|---|---|---|
| edge-01 | 200MB | 300MB | 2GB |
| iot-02 | 200MB | 700MB (HA大户) | 2GB |
| storage-03 | 200MB | 250MB | 2GB |

### 5.3 OOM 保护
```yaml
mem_limit: 512m  # Home Assistant
mem_limit: 128m  # AdGuard Home
mem_limit: 64m   # Memos / cloudflared
```
