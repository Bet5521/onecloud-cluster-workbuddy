#!/bin/bash
# Syncthing 初始化脚本

SYN_DIR="$(dirname "$0")"
mkdir -p "$SYN_DIR/config"
mkdir -p "$SYN_DIR/data"

# 设置权限 (Syncthing 在容器内以 1000 运行)
chown -R 1000:1000 "$SYN_DIR" 2>/dev/null || true

echo "[*] Syncthing 目录已初始化"
echo "[*] 启动容器后访问 http://${NODE_IP}:8384 完成 Web 配置"
echo ""
echo "[*] 推荐设置:"
echo "    - 添加管理密码 (Settings → General → GUI Authentication)"
echo "    - 设置设备名称: wk-storage-03"
echo "    - 创建共享文件夹: ~/Sync (对应 /var/syncthing/data/Sync)"
echo "    - 添加其他节点为 Remote Devices"
echo "    - 设置 Discovery 和 Relay (局域网直接连接)"
echo ""
echo "[*] 集群同步建议:"
echo "    - cluster-shared: 共享集群配置和备份"
echo "    - media-downloads: aria2 下载目录"
echo "    - photos-backup: Piwigo 相册备份"
echo "    - homeassistant-config: HA 配置热备"
