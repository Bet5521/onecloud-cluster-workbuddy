#!/bin/bash
# Home Assistant 初始化脚本

HA_DIR="$(dirname "$0")"

mkdir -p "$HA_DIR/custom_components"
mkdir -p "$HA_DIR/www"
mkdir -p "$HA_DIR/scripts"
mkdir -p "$HA_DIR/automations"
mkdir -p "$HA_DIR/packages"

# 创建空的 include 文件（HA 启动时会查找它们）
touch "$HA_DIR/automations.yaml"
touch "$HA_DIR/scripts.yaml"
touch "$HA_DIR/scenes.yaml"
touch "$HA_DIR/customize.yaml"

# 设置权限
chown -R 1000:1000 "$HA_DIR" 2>/dev/null || true

echo "[✓] Home Assistant 目录已初始化"
echo "[*] 首次启动后请访问 http://${NODE_IP}:8123 完成注册"
echo "[*] 建议: 在首次启动后关闭 'Cloud' 组件 (如不需要)"
