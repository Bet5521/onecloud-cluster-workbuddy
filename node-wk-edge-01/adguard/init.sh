#!/bin/bash
# AdGuard Home 初始化脚本
# 首次运行后访问 http://<ip>:3000 完成 WebUI 配置

AG_DIR="$(dirname "$0")"
mkdir -p "$AG_DIR/work" "$AG_DIR/conf"

echo "[*] AdGuard Home 目录已创建"
echo "[*] 启动容器后请访问 http://<ip>:3000 进行配置向导"
echo "[*] 建议配置:"
echo "    - 设置管理员密码"
echo "    - 启用 DHCP (可选, 如路由器已开DHCP则关闭)"
echo "    - 添加自定义过滤列表"
echo "    - 设置上游 DNS: https://dns.google-doh3.google.com/dns-query"
echo "    - 设置加密 DNS (DoT: port 853)"
