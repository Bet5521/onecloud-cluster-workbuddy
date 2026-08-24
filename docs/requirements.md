# 需求文档

## 1. 项目背景

用户拥有 3-4 台玩客云（OneCloud WS1608）设备，已刷入 Armbian 系统并预装 Docker。现计划组建轻量级自托管集群，集中部署 18 个自托管服务，实现家居生活中文件同步、智能家居、网络穿透、DNS过滤、下载管理、笔记、博客、图片管理、代码托管、打印等功能。

## 2. 硬件与网络环境

### 2.1 硬件规格
| 项目 | 规格 | 备注 |
|---|---|---|
| CPU | Amlogic S805 | ARMv7 (32位) |
| 内存 | 1GB DDR3 | 极其紧张 |
| 内置存储 | 8GB eMMC | 仅放系统 |
| 扩展存储 | 128GB SD Card | 挂载为Docker数据目录 |
| 网络 | 千兆网口 | 内网使用 |
| 系统 | Armbian (Debian 11/12) | Linux |
| Docker | 已预装 | Docker Engine |

### 2.2 网络环境
- 无公网IP，需通过 Cloudflare Tunnel 实现内网穿透
- 家庭局域网，路由器为网关
- 需要 WireGuard 组建节点间 mesh 通信

## 3. 功能需求

### 3.1 文件管理类
| 需求 | 服务 | 优先级 | 备注 |
|---|---|---|---|
| P2P文件同步 | Syncthing | 高 | 多节点互相同步 |
| 微力同步备选 | verysync | 中 | Syncthing不足时使用 |
| BT/HTTP下载 | aria2 | 高 | 下载到共享目录 |
| 图片管理 | Piwigo | 中 | Web相册 |
| 打印服务 | cupsd + cups-web | 中 | 局域网打印 |

### 3.2 智能家居类
| 需求 | 服务 | 优先级 | 备注 |
|---|---|---|---|
| 智能家居中枢 | Home Assistant | 高 | 自动化、设备控制 |
| 小米音乐播放 | xiaomusic | 中 | 原生客户端 |
| AI助手 | migpt | 低 | GPT 交互界面 |

### 3.3 网络与安全类
| 需求 | 服务 | 优先级 | 备注 |
|---|---|---|---|
| 内网穿透 | cloudflared | 高 | Cloudflare Tunnel |
| 家庭DNS+广告拦截 | AdGuard Home | 高 | 全家庭设备生效 |
| 节点间mesh网络 | WireGuard | 高 | 集群内部通信 |
| 代理客户端 | Clash/mihomo | 中 | 可选，全局科学上网 |

### 3.4 生产力类
| 需求 | 服务 | 优先级 | 备注 |
|---|---|---|---|
| 轻量笔记 | Memos | 高 | 多设备同步 |
| 个人博客 | Typecho | 中 | 轻量级博客系统 |
| 代码托管 | Gitea | 中 | 自托管Git仓库 |

### 3.5 集群管理
| 需求 | 实现方式 | 优先级 | 备注 |
|---|---|---|---|
| 集群控制面板 | Flask Web应用 | 高 | 状态总览、快捷操作 |
| 运维脚本 | Shell脚本 | 高 | 初始化/备份/健康检查 |
| 配置管理 | YAML inventory | 中 | 统一管理所有节点配置 |

## 4. 非功能需求

### 4.1 性能约束
- 单台节点内存不超过 900MB（含swap）
- Web服务响应时间 < 3s
- 集群总功耗 < 15W

### 4.2 可靠性要求
- 服务自重启（restart: unless-stopped）
- 配置每周自动备份
- Syncthing 多节点数据冗余
- 核心服务（AdGuard, cloudflared）优先启动

### 4.3 可维护性
- 所有配置文件集中管理
- Shell 脚本自动化部署和运维
- 标准化目录结构
- 清晰的文档说明

### 4.4 安全性
- SSH 使用密钥认证，禁用密码登录
- Docker 容器最小权限原则
- Cloudflare Tunnel 加密传输
- 服务端口按需暴露

## 5. 约束条件

### 5.1 架构约束
- 所有服务必须支持 ARMv7 (armhf) 架构
- 无法使用 x86_64 专用镜像

### 5.2 Docker 约束
- Docker Engine v28 是最后支持 armhf 的版本
- 需防止 Docker 自动升级到 v29+

### 5.3 存储约束
- SD卡使用寿命有限，避免频繁写入
- Docker 日志需配置轮转

## 6. 交付物

- 完整文档：需求/架构/拓扑/运维/Cloudflare
- inventory 清单：nodes.yaml + services.yaml
- 3 节点配置：docker-compose + .env + 服务初始化脚本
- 运维脚本：bootstrap/deploy/backup/restore/health-check/update/wireguard
- Flask 控制面板：app.py + templates + static
- README 主文档
