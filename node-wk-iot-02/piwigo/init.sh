#!/bin/bash
# Piwigo 初始化脚本

PIWIGO_DIR="$(dirname "$0")"

mkdir -p "$PIWIGO_DIR/config"
mkdir -p "$PIWIGO_DIR/gallery"
mkdir -p "$PIWIGO_DIR/gallery/users"

# 设置权限
chown -R 1000:1000 "$PIWIGO_DIR" 2>/dev/null || true

echo "[*] Piwigo 目录已初始化"
echo "[*] 启动容器后访问 http://${NODE_IP}:8080 完成 Web 安装向导"
echo "[*] 数据库: 选择内置 SQLite (最简单)"
echo "[*] 管理员账号设置后可以开始上传照片"
echo ""
echo "[*] 推荐插件 (在 WebUI 中安装):"
echo "    - PWG Stuffs (增强界面)"
echo "    - Piwigo Download Manager"
echo "    - Lightbox"
