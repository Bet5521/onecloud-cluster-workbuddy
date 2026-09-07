# 🔵 node-wk-iot-02 — IoT Core 节点

IoT 核心节点，运行智能家居平台、相册、博客和音乐播放等服务。

---

## 节点信息

| 项目 | 值 |
|------|-----|
| 主机名 | iot-02 |
| IP | 192.168.1.102 |
| WireGuard IP | 10.8.0.102 |
| 角色 | iot-core |

---

## 服务列表

| 服务 | 类型 | 端口 | 说明 |
|------|------|------|------|
| Home Assistant | Docker | 8123 | 开源智能家居平台 |
| Piwigo | Docker | 8080 | 网页相册管理 |
| Typecho | Docker | 8083 | 轻量博客系统 |
| xiaomusic | 原生 | 8081 | 小爱音箱音乐播放器 |
| migpt | 原生 | 8082 | 小米 AI 对话代理 |

---

## 文件说明

```
node-wk-iot-02/
├── docker-compose.yml          # Docker 服务编排（homeassistant, piwigo, typecho）
├── .env.example                # 环境变量模板
├── homeassistant/
│   ├── configuration.yaml      # HA 配置文件
│   └── init.sh                 # HA 初始化脚本
├── migpt/
│   ├── config.yaml             # migpt 配置
│   ├── install.sh              # migpt 安装入口（委派 scripts/install-services.sh）
│   ├── proxy.py                # Flask API 代理脚本
│   └── requirements.txt        # Python 依赖
├── piwigo/
│   └── init.sh                 # Piwigo 初始化脚本
└── xiaomusic/
    ├── config.json             # xiaomusic 配置
    ├── install.sh              # xiaomusic 安装入口（委派 scripts/install-services.sh）
    └── install-service.sh      # systemd 服务安装入口
```

---

## 快速部署

```bash
# 1. 初始化节点
./scripts/bootstrap.sh --node wk-iot-02 --yes

# 2. 分发配置
./scripts/deploy.sh -n wk-iot-02

# 3. 启动 Docker 服务
./scripts/install-services.sh iot

# 4. 安装原生服务
./scripts/install-services.sh xiaomusic
./scripts/install-services.sh migpt
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
| `HA_IMAGE` | Home Assistant 镜像（默认 armv7 版本） |
| `PIWIGO_PORT` | Piwigo 端口（默认 8080） |
| `WG_PEER01_PUBKEY` | WireGuard Peer 01 公钥 |
| `WG_PEER03_PUBKEY` | WireGuard Peer 03 公钥 |
