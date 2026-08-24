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
cd /mnt/sd/<node-name>/
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
./scripts/restore.sh 20260814_030000 homeassistant
```

## 4. 更新策略

### 4.1 Docker 镜像更新
```bash
cd /mnt/sd/<node-name>/
docker-compose pulldocker-compose up -d
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

## 8. 节点重建

```bash
git clone <your-repo> onecloud-cluster
cd onecloud-cluster
./scripts/bootstrap.sh --node wk-edge-01 --ip 192.168.1.101
./scripts/restore.sh latest.tar.gz
```
