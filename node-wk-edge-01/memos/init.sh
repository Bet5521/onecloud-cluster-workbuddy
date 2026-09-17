#!/bin/bash
# Memos 初始化脚本
# 首次部署时运行一次

DATA_DIR="$(dirname "$0")/data"
mkdir -p "$DATA_DIR"

# 设置正确的权限 (Memos 使用 UID 10001)
chown -R 10001:10001 "$DATA_DIR" 2>/dev/null || true

echo "[*] Memos 数据目录已准备: $DATA_DIR"
echo "[*] 首次启动后请访问 http://<ip>:5230 完成初始化"
echo "[*] 如需设置公开访问 URL, 在 .env 中设置 MEMOS_INSTANCE_URL=https://memos.yourdomain.com"
