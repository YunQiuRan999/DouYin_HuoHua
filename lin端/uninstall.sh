#!/usr/bin/env bash
# ============================================================
# 抖音自动续火花 - 一键卸载脚本
# 用法：sudo bash uninstall.sh  或  bash uninstall.sh --user
# ============================================================
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

SERVICE_NAME="douyin-fire"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_MODE=false

for arg in "$@"; do
    [ "$arg" = "--user" ] && USER_MODE=true
done

echo -e "${RED}⚠  即将卸载抖音续火花服务${NC}"
echo "  部署目录: $SCRIPT_DIR"
echo ""
read -rp "确认卸载？此操作会停止并删除服务（数据文件保留）[y/N]: " confirm
[ "$confirm" != "y" ] && [ "$confirm" != "Y" ] && { echo "已取消"; exit 0; }

if [ "$USER_MODE" = true ]; then
    SYSTEMCTL="systemctl --user"
    UNIT_FILE="$HOME/.config/systemd/user/${SERVICE_NAME}.service"
else
    SUDO=""
    [ "$(id -u)" != "0" ] && SUDO="sudo"
    SYSTEMCTL="$SUDO systemctl"
    UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
fi

echo "停止服务..."
$SYSTEMCTL stop "$SERVICE_NAME" 2>/dev/null || true
$SYSTEMCTL disable "$SERVICE_NAME" 2>/dev/null || true

echo "删除服务单元..."
rm -f "$UNIT_FILE" 2>/dev/null || $SUDO rm -f "$UNIT_FILE" 2>/dev/null || true

$SYSTEMCTL daemon-reload 2>/dev/null || true

echo -e "${GREEN}卸载完成！${NC}"
echo ""
echo "数据文件已保留，如需彻底删除："
echo "  rm -rf $SCRIPT_DIR"
echo "  rm -rf ~/.cache/ms-playwright  (Playwright浏览器缓存)"
