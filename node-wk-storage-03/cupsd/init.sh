#!/bin/bash
# CUPS 初始化脚本

CUPS_DIR="$(dirname "$0")"
mkdir -p "$CUPS_DIR/config" "$CUPS_DIR/printers" "$CUPS_DIR/spool"

chown -R 1000:1000 "$CUPS_DIR" 2>/dev/null || true

echo "[*] CUPS 目录已初始化"
echo "[*] 启动后访问:"
echo "    CUPS Web:   http://${NODE_IP}:631 (管理员: ${CUPS_ADMIN_USER})"
echo "    CUPS WebUI: http://${NODE_IP}:632 (更简洁的界面)"
echo ""
echo "[*] 添加打印机步骤:"
echo "    1. 访问 http://${NODE_IP}:631/admin"
echo "    2. 点击 'Add Printer'"
echo "    3. 选择 'IPP (Internet Printing Protocol)'"
echo "    4. URI: ipp://printer-ip/ipp/print"
echo "    5. 选择驱动或使用 PPD 文件"
echo ""
echo "[*] 从其他设备打印:"
echo "    - Windows: 添加打印机, URL = http://${NODE_IP}:631/printers/队列名"
echo "    - macOS: 添加打印机, 选择 'Internet Printing Protocol'"
echo "    - Linux: lpadmin -p MyPrinter -E -v ipp://${NODE_IP}:631/printers/队列名 -m everywhere"
