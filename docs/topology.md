# 拓扑图文档

## 1. Mermaid 架构拓扑图

### 1.1 完整集群拓扑
```mermaid
graph TD
    subgraph Cloudflare["Cloudflare Zero Trust"]
        Tunnel["Cloudflare Tunnel"]
    end
    subgraph Internet["公网"]
        User["用户设备"]
    end
    subgraph Cluster["玩客云集群"]
        subgraph Edge["NODE-01: Edge Gateway"]
            CF[cloudflared]
            AG["AdGuard Home<br>:53/:3000"]
            WG["WireGuard :51820"]
            CL[Clash/mihomo]
            MEM["Memos :5230"]
            PANEL["面板 :9000"]
        end
        subgraph IoT["NODE-02: IoT Core"]
            HA["Home Assistant :8123"]
            XM["xiaomusic :8081"]
            MG["migpt :8082"]
            PIW["Piwigo :8080"]
            TYP["Typecho :8083"]
        end
        subgraph Storage["NODE-03: Storage & Sync"]
            SYN["Syncthing :8384/:22000"]
            VSY["verysync :19900"]
            ARA["aria2 :6800/:6888"]
            CUPS["CUPS :631"]
            GIT["Gitea :3000"]
        end
    end
    User --> Tunnel --> CF
    CF --> AG & MEM & PANEL & HA & PIW
    WG <--> HA & SYN
    style Cloudflare fill:#f38020,color:#fff
    style Edge fill:#4CAF50,color:#fff
    style IoT fill:#2196F3,color:#fff
    style Storage fill:#FF9800,color:#fff
```

### 1.2 网络层拓扑
```mermaid
graph LR
    subgraph LocalNet["家庭局域网 192.168.1.0/24"]
        Router[路由器 192.168.1.1]
        WK1[wk-edge-01 10.8.0.101]
        WK2[wk-iot-02 10.8.0.102]
        WK3[wk-storage-03 10.8.0.103]
    end
    Router --> WK1 & WK2 & WK3
    WK1 <-->|WG mesh| WK2
    WK1 <-->|WG mesh| WK3
    WK2 <-->|WG mesh| WK3
```

## 2. ASCII 拓扑图

```
╔════════════════════════════════════════════════════════════════╗
║                    ONE CLOUD CLUSTER v1.0                       ║
║                                                                  ║
║   ┌──────────────────────────────────────────────────────────┐   ║
║   │                  Cloudflare Zero Trust                  │   ║
║   └──────────────────────────────────────────────────────────┘   ║
║                              │                                   ║
║                              ▼                                   ║
║   ┌─────────────────┐  WG MESH  ┌─────────────────┐              ║
║   │ NODE-01: EDGE   │◄──────────►│ NODE-02: IOT    │              ║
║   │ .101 / 10.8.0.101│           │ .102 / 10.8.0.102│              ║
║   │ cloudflared      │           │ Home Assistant   │              ║
║   │ AdGuard Home     │           │ Piwigo / Typecho │              ║
║   │ WireGuard        │           │ xiaomusic/migpt  │              ║
║   │ Memos/Clash      │           │                  │              ║
║   └────────┬─────────┘           └────────┬─────────┘              ║
║            └──────────┬──────────────────┘                       ║
║                       ▼                                          ║
║           ┌───────────────────────────┐                          ║
║           │ NODE-03: STORAGE & SYNC  │                          ║
║           │ .103 / 10.8.0.103        │                          ║
║           │ Syncthing / aria2        │                          ║
║           │ CUPS / Gitea / verysync  │                          ║
║           └───────────────────────────┘                          ║
╚════════════════════════════════════════════════════════════════╝
```
