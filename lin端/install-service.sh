#!/usr/bin/env bash
# ============================================================
# 抖音自动续火花 - systemd 自启安装脚本
# 注册 systemd 服务：开机自启 + 崩溃自动重启
#
# 用法（推荐，系统级）：
#   sudo bash install-service.sh
# 用法（无 sudo 权限，用户级）：
#   bash install-service.sh --user
# ============================================================
set -e
cd "$(dirname "$0")"

SERVICE_NAME="douyin-fire"
SERVICE_DIR="$(pwd)"
PYTHON_BIN="$SERVICE_DIR/.venv/bin/python"

if [ ! -x "$PYTHON_BIN" ]; then
    echo "未找到虚拟环境，请先运行 ./start.sh 完成初始化。"
    exit 1
fi

if [ "$1" = "--user" ]; then
    UNIT_DIR="$HOME/.config/systemd/user"
    MODE="user"
    mkdir -p "$UNIT_DIR"
else
    if [ "$(id -u)" != "0" ]; then
        echo "系统级安装需要 root，请使用：sudo bash install-service.sh  或  bash install-service.sh --user"
        exit 1
    fi
    UNIT_DIR="/etc/systemd/system"
    MODE="system"
fi

UNIT_FILE="$UNIT_DIR/${SERVICE_NAME}.service"
RUN_AS_USER="${SUDO_USER:-$USER}"
if [ "$RUN_AS_USER" = "root" ] || [ -z "$RUN_AS_USER" ]; then
    RUN_AS_USER="$USER"
fi

if [ "$MODE" = "user" ]; then
    cat > "$UNIT_FILE" <<EOF
[Unit]
Description=Douyin Auto Fire Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$SERVICE_DIR
ExecStart=$PYTHON_BIN main.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF
else
    cat > "$UNIT_FILE" <<EOF
[Unit]
Description=Douyin Auto Fire Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_AS_USER
WorkingDirectory=$SERVICE_DIR
ExecStart=$PYTHON_BIN main.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
fi

echo "已生成服务单元: $UNIT_FILE"

if [ "$MODE" = "user" ]; then
    systemctl --user daemon-reload
    systemctl --user enable --now "$SERVICE_NAME"
    loginctl enable-linger "$USER" 2>/dev/null || true
    systemctl --user status "$SERVICE_NAME" --no-pager || true
    echo ""
    echo "用户级服务已启用（开机自启 + 崩溃重启）。常用命令："
    echo "  systemctl --user status $SERVICE_NAME   查看状态"
    echo "  systemctl --user restart $SERVICE_NAME  重启"
    echo "  journalctl --user -u $SERVICE_NAME -f   实时日志"
else
    systemctl daemon-reload
    systemctl enable --now "$SERVICE_NAME"
    systemctl status "$SERVICE_NAME" --no-pager || true
    echo ""
    echo "系统级服务已启用（开机自启 + 崩溃重启）。常用命令："
    echo "  systemctl status $SERVICE_NAME   查看状态"
    echo "  systemctl restart $SERVICE_NAME  重启"
    echo "  journalctl -u $SERVICE_NAME -f   实时日志"
fi
