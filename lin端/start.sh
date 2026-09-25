#!/usr/bin/env bash
# ============================================================
# 抖音自动续火花 - Linux 服务一键启动脚本
# 自动完成：venv 创建 -> 依赖安装 -> Playwright 浏览器安装 -> 启动
# 用法：./start.sh            （前台运行）
#       ./start.sh --daemon   （后台运行，日志写入 data/logs/）
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8765}"
PYTHON="${PYTHON:-python3}"

echo "[1/5] 检查 Python..."
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "错误：未找到 python3，请先安装：sudo apt install python3 python3-venv python3-pip"
    exit 1
fi
echo "  Python 版本: $($PYTHON --version 2>&1)"

echo "[2/5] 检查端口占用..."
if command -v ss >/dev/null 2>&1; then
    if ss -tlnp 2>/dev/null | grep -q ":$PORT "; then
        echo "警告：端口 $PORT 已被占用，服务可能无法启动"
    fi
elif command -v netstat >/dev/null 2>&1; then
    if netstat -tlnp 2>/dev/null | grep -q ":$PORT "; then
        echo "警告：端口 $PORT 已被占用，服务可能无法启动"
    fi
fi

echo "[3/5] 创建虚拟环境并安装依赖..."
if [ ! -d .venv ]; then
    "$PYTHON" -m venv .venv
    echo "  虚拟环境已创建"
fi
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q
echo "  依赖安装完成"

echo "[4/5] 安装 Playwright 浏览器（首次约 200MB）..."
if [ ! -d "$HOME/.cache/ms-playwright" ] || [ -z "$(ls -A "$HOME/.cache/ms-playwright" 2>/dev/null)" ]; then
    if [ "$(id -u)" = "0" ]; then
        .venv/bin/playwright install chromium --with-deps
    else
        echo "  （非 root，跳过系统依赖；如浏览器启动失败请以 root 运行一次：sudo .venv/bin/playwright install --with-deps chromium）"
        .venv/bin/playwright install chromium
    fi
else
    echo "  已检测到 Playwright 浏览器缓存，跳过安装。"
fi

echo "[5/5] 启动服务..."
if [ "${1:-}" = "--daemon" ]; then
    mkdir -p data/logs
    nohup .venv/bin/python main.py --port "$PORT" >> data/logs/server.out.log 2>&1 &
    echo "服务已在后台启动：http://0.0.0.0:${PORT}  （日志: data/logs/server.out.log）"
    echo "Token 见 config.json，或使用：./start.sh 后查看启动输出。"
else
    exec .venv/bin/python main.py --port "$PORT"
fi
