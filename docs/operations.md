# 运维手册

## 1. 日常运维

### 1.1 登录节点
```bash
ssh root@wk-edge-01
ssh root@wk-iot-02
ssh root@wk-storage-03
```

### 1.2 查看服务状态
```bash
docker ps -a
docker stats --no-stream
systemctl status mihomo xiaomusic migpt verysync
wg show
free -h && df -h
```

### 1.3 日志查看
```bash
docker logs --tail 200 <container>
journalctl -u <service> -f --no-pager
dmesg -T | tail -50
```

## 2. 服务管理

### 2.1 Docker Compose
```bash
cd /mnt/sd/srv/<node-name>/    # 如 /mnt/sd/srv/wk-edge-01
docker-compose up -d
docker-compose down
docker-compose restart <svc>
docker-compose pull
```

### 2.2 原生二进制
```bash
systemctl daemon-reload
systemctl enable mihomo
systemctl start mihomo
```

## 3. 备份与恢复

### 3.1 手动备份
```bash
./scripts/backup.sh all
./scripts/backup.sh node edge-01
./scripts/backup.sh service homeassistant
./scripts/backup.sh service typecho
./scripts/backup.sh service gitea
```

### 3.2 定时备份
```bash
crontab -e
0 3 * * 0 /mnt/sd/scripts/backup.sh all >> /var/log/backup.log 2>&1
```

### 3.3 恢复
```bash
./scripts/restore.sh 20260814_030000 homeassistant     # 简写: 自动识别为服务
./scripts/restore.sh 20260814_030000 iot-02            # 简写: 自动识别为节点
./scripts/restore.sh 20260814_030000 service homeassistant
./scripts/restore.sh 20260814_030000 node wk-iot-02
./scripts/restore.sh latest all
```
> 备份 ID 是 `backup.sh` 生成的时间戳目录名 (如 `20260814_030000`),
> 位于各节点的 `/mnt/sd/backups/` 下, `latest` 表示最新一份。

## 4. 更新策略

### 4.1 Docker 镜像更新
```bash
cd /mnt/sd/srv/<node-name>/
docker-compose pull
docker-compose up -d
```

也可直接批量更新所有节点:
```bash
./scripts/update-all.sh            # 仅更新镜像 (默认)
./scripts/update-all.sh --system   # 仅更新系统包 (排除 Docker Engine)
./scripts/update-all.sh --all      # 两者都更新
./scripts/update-all.sh -n wk-iot-02
```

### 4.2 系统更新（注意不要升级 Docker 到 v29+）
```bash
apt update && apt upgrade
apt-mark hold docker.io docker-ce docker-ce-cli containerd.io
```

## 5. WireGuard 维护

### 5.1 查看连接
```bash
wg show
watch -n 1 wg show
```

### 5.2 添加新节点
```bash
./scripts/wireguard-setup.sh add peer wk-backup-04 192.168.1.104 10.8.0.104
./scripts/wireguard-setup.sh list          # 查看已登记节点
./scripts/wireguard-setup.sh               # 重新生成全部节点 wg0.conf
```
> 生成的配置写入各 `node-<名称>/wireguard/wg0.conf` (含私钥, 已被 .gitignore 忽略),
> 运行 `./scripts/deploy.sh` 即可分发到对应节点。

### 5.3 部署配置到节点
```bash
cp node-<名称>/wireguard/wg0.conf /etc/wireguard/wg0.conf
chmod 600 /etc/wireguard/wg0.conf
systemctl enable --now wg-quick@wg0
```

## 6. 常见故障排查

### 6.1 节点无法SSH
```bash
ping 192.168.1.101
arp -a
ls /mnt/sd  # 确认SD卡已挂载
```

### 6.2 Docker 容器不断重启
```bash
docker logs <container>
chown -R 1000:1000 <volume_path>
ss -tlnp | grep <port>
```

### 6.3 Home Assistant OOM
```bash
dmesg | grep -i oom
swapon --show
```

### 6.4 Cloudflare Tunnel 断开
```bash
docker logs cloudflared
docker-compose restart cloudflared
```

### 6.5 Syncthing 同步失败
```bash
# WebUI → Actions → Scan All Now
```

### 6.6 SD卡只读
```bash
umount /mnt/sd
fsck.ext4 /dev/mmcblk1p1
```

## 7. 安全注意事项

1. root SSH 密钥不要外泄
2. Cloudflare Tunnel Token 保密
3. 定期更换 WireGuard 密钥（每季度）
4. AdGuard/Clash/aria2 Secret 使用强密码
5. 不要开放不必要的外部端口

### 7.1 防火墙（v1.5.0 起：部署脚本不碰）

**本项目的部署脚本不改任何节点的防火墙** —— 不写 `iptables`/`ip6tables`，
不下发 `ufw`/`firewall-cmd`/`nft`，生成 `wg0.conf` 时也**默认不带** `PostUp`/`PostDown`
规则。改防火墙只有一个入口：你手动执行 `setup_firewall.sh`。

部署完节点后生成「该放行哪些端口」的建议清单：

```bash
./scripts/firewall-recommend.sh              # 全部节点 -> docs/firewall/<节点>.txt
./scripts/firewall-recommend.sh --emit-dsl   # 只打印可直接录入的规则行
```

清单是**静态推算**（读 `inventory/nodes.yaml` + `inventory/services.yaml`），
不发网络请求、不连节点。内容分三块：

1. **必需规则**：SSH、控制面板、WireGuard（`udp/51820`）
2. **内网规则**：AdGuard `53`、Grafana `3000` 等只对 LAN 开放的端口
3. **Hub 节点附加段**：WireGuard 转发/NAT（`sysctl ip_forward`、`FORWARD`、
   `MASQUERADE`）—— DSL 表达不了，需手工录入

变量端口（如 `${MEMOS_PORT}`）会单独列出并标注"需人工确认"，不会被静默丢弃。

拿到清单后，到对应节点上：把规则行录进 `setup_firewall.sh` →
`/etc/fw-setup/rules.dsl` → 应用。**应用前务必确认 SSH 放行规则已生效**，
否则 INPUT 置 DROP 会把自己关在门外。

> 特殊需求：确实要让 WireGuard 自带 iptables 规则（老教程的用法），
> 用 `ONECLOUD_WG_FIREWALL=1 ./scripts/wireguard-setup.sh gen` 显式打开，
> 但那会绕开统一入口，需自行确认与清单不冲突。

## 8. 节点重建

```bash
git clone <your-repo> onecloud-cluster
cd onecloud-cluster
./scripts/bootstrap.sh --node wk-edge-01 --ip 192.168.1.101 --hostname edge-01 --yes
./scripts/deploy.sh -n wk-edge-01
./scripts/restore.sh latest all
```
