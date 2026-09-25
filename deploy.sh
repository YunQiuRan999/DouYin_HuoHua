#!/usr/bin/env bash
# ============================================================
#  抖音自动续火花 · Linux 服务器端 一键部署脚本
#  文件名：deploy.sh
#
#  用法（上传到服务器后执行）：
#     sudo bash deploy.sh                # 系统级部署（推荐）
#     bash deploy.sh --user              # 没有 root 权限时用
#     sudo bash deploy.sh --port 8765    # 指定端口（默认 8765）
#     sudo bash deploy.sh --src /root/lin端   # 手动指定源码目录
#
#  脚本会自动完成：
#     1. 定位服务器端源码目录（不依赖中文目录名，乱码也能找到）
#     2. 安装系统依赖（python3 / venv / Chromium 运行库）
#     3. 创建虚拟环境并安装 Python 依赖
#     4. 安装 Playwright Chromium 浏览器内核
#     5. 生成配置文件（自动生成连接 Token）
#     6. 注册 systemd 服务（开机自启 + 崩溃自动重启）
#     7. 放行防火墙端口
#     8. 启动服务并健康检查，最后打印连接信息
# ============================================================
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

info() { echo -e "${BLUE}[信息]${NC} $*"; }
ok()   { echo -e "${GREEN}[成功]${NC} $*"; }
warn() { echo -e "${YELLOW}[注意]${NC} $*"; }
err()  { echo -e "${RED}[错误]${NC} $*"; }
step() { echo -e "\n${CYAN}========== $* ==========${NC}"; }

usage() {
    echo "用法: sudo bash deploy.sh [选项]"
    echo ""
    echo "选项:"
    echo "  --user            无 root 权限时使用用户级服务"
    echo "  --port <端口>     指定服务端口（默认读 config.json，否则 8765）"
    echo "  --src <目录>      手动指定服务器端源码目录"
    echo "  -h, --help        显示本帮助"
}

# 若脚本被 Windows 编辑成 CRLF 换行，会自动修复后重新执行，
# 避免出现 "bash: $'\r': command not found" 这类报错。
if [ -z "${_DEPLOY_CRLF_FIXED:-}" ] && grep -q $'\r' "$0" 2>/dev/null; then
    if sed -i 's/\r$//' "$0" 2>/dev/null; then
        exec env _DEPLOY_CRLF_FIXED=1 bash "$0" "$@"
    fi
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="douyin-fire"
USER_MODE=false
SRC_ARG=""
PORT_ARG=""

while [ $# -gt 0 ]; do
    case "$1" in
        --user)  USER_MODE=true; shift ;;
        --port)  [ $# -ge 2 ] || { err "--port 后面需要跟端口号"; exit 1; }; PORT_ARG="$2"; shift 2 ;;
        --src)   [ $# -ge 2 ] || { err "--src 后面需要跟目录路径"; exit 1; }; SRC_ARG="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) err "未知参数：$1（用 -h 查看帮助）"; exit 1 ;;
    esac
done

if [ "$(id -u)" = "0" ]; then SUDO=""; else SUDO="sudo"; fi

# ------------------------------------------------------------
# 定位服务器端源码目录
# 判断标准：含 main.py + server/core.py + requirements.txt，
# 且不含 ui/ 目录（用于排除 Windows 端源码）。
# ------------------------------------------------------------
is_lin_dir() {
    local d="$1"
    [ -n "$d" ] && [ -d "$d" ] || return 1
    [ -f "$d/main.py" ] || return 1
    [ -f "$d/server/core.py" ] || return 1
    [ -f "$d/requirements.txt" ] || return 1
    [ -d "$d/ui" ] && return 1
    return 0
}

resolve_src() {
    local cand marker
    if [ -n "$SRC_ARG" ]; then
        if is_lin_dir "$SRC_ARG"; then (cd "$SRC_ARG" && pwd); return 0; fi
        err "--src 指定的目录不是有效的服务器端源码：$SRC_ARG"
        return 1
    fi
    # 情况一：deploy.sh 被放进源码目录里一起上传
    if is_lin_dir "$SCRIPT_DIR"; then echo "$SCRIPT_DIR"; return 0; fi
    # 情况二：源码在脚本同级或下一级目录（按内容查找，中文目录名乱码也能命中）
    for marker in install-service.sh main.py; do
        while IFS= read -r cand; do
            [ -n "$cand" ] || continue
            if is_lin_dir "$cand"; then echo "$cand"; return 0; fi
        done < <(find "$SCRIPT_DIR" -maxdepth 3 -type f -name "$marker" -print0 2>/dev/null | xargs -0 -r -n1 dirname 2>/dev/null | sort -u)
    done
    # 情况三：源码在脚本的上一级目录
    cand="$(cd "$SCRIPT_DIR/.." && pwd)"
    if is_lin_dir "$cand"; then echo "$cand"; return 0; fi
    return 1
}

echo -e "${CYAN}"
echo "  =================================================="
echo "   抖音自动续火花 · Linux 服务器端 一键部署"
echo "  =================================================="
echo -e "${NC}"

info "正在查找服务器端源码目录…"
if ! SRC_DIR="$(resolve_src)"; then
    err "没有找到服务器端源码目录"
    echo "  源码目录应包含：main.py、server/core.py、requirements.txt"
    echo "  请确认 deploy.sh 与 lin端 文件夹在同一目录下，或手动指定："
    echo "      sudo bash deploy.sh --src /root/lin端"
    exit 1
fi
ok "源码目录：$SRC_DIR"

VENV_DIR="$SRC_DIR/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"

# 浏览器内核固定装到源码目录下：sudo 的 HOME 与 systemd 运行用户的 HOME
# 往往不一致，用默认缓存路径会导致服务启动后找不到 Chromium。
PW_BROWSERS="$SRC_DIR/.playwright-browsers"
export PLAYWRIGHT_BROWSERS_PATH="$PW_BROWSERS"

# 修复 Windows 上传导致的 CRLF 换行（否则启动脚本会报错）
find "$SRC_DIR" -maxdepth 1 -type f -name "*.sh" -exec sed -i 's/\r$//' {} \; 2>/dev/null || true

info "部署模式：$([ "$USER_MODE" = true ] && echo '用户级 systemd（无需 root）' || echo '系统级 systemd（开机自启）')"

# ============================================================
# 步骤 1：检查 Python 环境
# ============================================================
step "步骤 1/7  检查 Python 环境"

PKG_MANAGER="unknown"
if command -v apt-get >/dev/null 2>&1; then
    PKG_MANAGER="apt"
elif command -v dnf >/dev/null 2>&1; then
    PKG_MANAGER="dnf"
elif command -v yum >/dev/null 2>&1; then
    PKG_MANAGER="yum"
fi

if ! command -v python3 >/dev/null 2>&1; then
    warn "未检测到 python3，尝试自动安装…"
    case "$PKG_MANAGER" in
        apt) $SUDO apt-get update -qq && $SUDO apt-get install -y -qq python3 python3-venv python3-pip ;;
        dnf) $SUDO dnf install -y python3 python3-pip ;;
        yum) $SUDO yum install -y python3 python3-pip ;;
        *)   err "未识别的系统，请手动安装 python3（3.9 及以上）后重试"; exit 1 ;;
    esac
fi

command -v python3 >/dev/null 2>&1 || { err "python3 安装失败，请手动安装后重试"; exit 1; }
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    err "Python 版本过低（需要 3.9 及以上）：$(python3 --version 2>&1)"
    exit 1
fi
ok "Python 可用：$(python3 --version 2>&1)"

if ! python3 -c 'import venv' >/dev/null 2>&1; then
    warn "缺少 venv 模块，尝试安装…"
    case "$PKG_MANAGER" in
        apt) $SUDO apt-get install -y -qq python3-venv ;;
        dnf|yum) $SUDO $PKG_MANAGER install -y python3-devel ;;
        *) warn "请手动安装 python3-venv 后重试" ;;
    esac
fi

AVAILABLE_KB=$(df -k "$SRC_DIR" 2>/dev/null | awk 'NR==2{print $4}' || echo "0")
if [ "${AVAILABLE_KB:-0}" -lt 512000 ] 2>/dev/null; then
    warn "磁盘可用空间不足 500MB（当前约 $((AVAILABLE_KB / 1024))MB），浏览器内核可能装不下"
fi

# ============================================================
# 步骤 2：创建虚拟环境并安装依赖
# ============================================================
step "步骤 2/7  创建虚拟环境并安装 Python 依赖"

if [ -d "$VENV_DIR" ]; then
    ok "虚拟环境已存在，直接复用：$VENV_DIR"
else
    info "创建虚拟环境（.venv，隔离依赖不污染系统）…"
    python3 -m venv "$VENV_DIR" || { err "创建虚拟环境失败，请确认 python3-venv 已安装"; exit 1; }
    ok "虚拟环境创建完成"
fi

"$PYTHON_BIN" -m pip install --upgrade pip -q
info "安装项目依赖（fastapi / uvicorn / playwright 等，首次约 1-2 分钟）…"
"$PYTHON_BIN" -m pip install -r "$SRC_DIR/requirements.txt" -q || { err "依赖安装失败，请检查网络后重试"; exit 1; }
ok "Python 依赖安装完成"

# ============================================================
# 步骤 3：安装 Playwright 浏览器内核
# ============================================================
step "步骤 3/7  安装 Playwright Chromium 浏览器内核"

PW_CACHE="$PW_BROWSERS"
info "浏览器内核目录：$PW_CACHE"
if [ -d "$PW_CACHE" ] && [ -n "$(ls -A "$PW_CACHE" 2>/dev/null)" ]; then
    ok "已检测到浏览器内核，跳过下载"
else
    info "下载 Chromium（约 150-200MB，请耐心等待）…"
    if [ "$(id -u)" = "0" ]; then
        "$VENV_DIR/bin/playwright" install chromium --with-deps >/dev/null 2>&1 \
            || { warn "自动安装系统依赖失败，改为仅下载浏览器内核…"; "$VENV_DIR/bin/playwright" install chromium || { err "Chromium 安装失败，请检查网络"; exit 1; }; }
    else
        "$VENV_DIR/bin/playwright" install chromium >/dev/null 2>&1 \
            || { err "Chromium 安装失败，请检查网络"; exit 1; }
        warn "非 root 运行，若浏览器启动缺系统库，请执行：sudo $VENV_DIR/bin/playwright install-deps chromium"
    fi
    ok "Chromium 安装完成"
fi

# ============================================================
# 步骤 4：生成配置文件
# ============================================================
step "步骤 4/7  生成配置文件"

cd "$SRC_DIR"
if [ ! -f "$SRC_DIR/config.json" ]; then
    info "生成全新的 config.json（含随机 Token）…"
    "$PYTHON_BIN" - <<PYEOF
import json, secrets
cfg = {
    "token": secrets.token_hex(16),
    "host": "0.0.0.0",
    "port": int("${PORT_ARG:-8765}"),
    "data_dir": "data",
    "scheduler_enabled": True,
    "cors_origins": ["http://127.0.0.1", "http://localhost"],
    "accounts": [],
}
with open("config.json", "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
PYEOF
    ok "config.json 已生成"
else
    ok "检测到已有 config.json，保留原有配置（Token 不变）"
fi

TOKEN="$("$PYTHON_BIN" -c "import json;print(json.load(open('config.json')).get('token',''))")"
CFG_PORT="$("$PYTHON_BIN" -c "import json;print(json.load(open('config.json')).get('port',8765))")"
PORT="${PORT_ARG:-$CFG_PORT}"

if [ -z "$TOKEN" ]; then
    TOKEN="$("$PYTHON_BIN" - <<'PYEOF'
import json, secrets
with open("config.json", encoding="utf-8") as f:
    cfg = json.load(f)
if not cfg.get("token"):
    cfg["token"] = secrets.token_hex(16)
    with open("config.json", "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
print(cfg["token"])
PYEOF
)"
    ok "已为空白 Token 生成新值"
fi

mkdir -p "$SRC_DIR/data/storage_state" "$SRC_DIR/data/qr" "$SRC_DIR/data/artifacts" "$SRC_DIR/data/tasks" "$SRC_DIR/data/logs"
chmod 700 "$SRC_DIR/data/storage_state" 2>/dev/null || true
ok "数据目录已就绪，服务端口：$PORT"

# ============================================================
# 步骤 5：注册 systemd 服务
# ============================================================
step "步骤 5/7  注册 systemd 服务（开机自启 + 崩溃自动重启）"

RUN_AS_USER="${SUDO_USER:-$USER}"
if [ "$USER_MODE" = true ]; then
    RUN_AS_USER="$USER"
elif [ "$RUN_AS_USER" = "root" ] || [ -z "$RUN_AS_USER" ]; then
    RUN_AS_USER="$(stat -c '%U' "$SRC_DIR" 2>/dev/null || echo root)"
fi

if [ "$USER_MODE" = true ]; then
    UNIT_DIR="$HOME/.config/systemd/user"
    SYSTEMCTL="systemctl --user"
    mkdir -p "$UNIT_DIR"
    WANTED_BY="default.target"
else
    UNIT_DIR="/etc/systemd/system"
    SYSTEMCTL="$SUDO systemctl"
    WANTED_BY="multi-user.target"
fi
UNIT_FILE="$UNIT_DIR/${SERVICE_NAME}.service"

if [ "$USER_MODE" = true ]; then
    cat > "$UNIT_FILE" <<EOF
[Unit]
Description=Douyin Auto Fire Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$SRC_DIR
ExecStart=$PYTHON_BIN $SRC_DIR/main.py --port $PORT
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=PLAYWRIGHT_BROWSERS_PATH=$PW_BROWSERS

[Install]
WantedBy=$WANTED_BY
EOF
else
    $SUDO tee "$UNIT_FILE" >/dev/null <<EOF
[Unit]
Description=Douyin Auto Fire Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_AS_USER
WorkingDirectory=$SRC_DIR
ExecStart=$PYTHON_BIN $SRC_DIR/main.py --port $PORT
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=PLAYWRIGHT_BROWSERS_PATH=$PW_BROWSERS

[Install]
WantedBy=$WANTED_BY
EOF
    if [ "$RUN_AS_USER" = "root" ]; then
        warn "未能确定非 root 运行用户，服务将以 root 运行（不推荐）"
    fi
    $SUDO chown -R "$RUN_AS_USER":"$RUN_AS_USER" "$SRC_DIR" 2>/dev/null || warn "目录授权失败，服务可能无写入权限"
fi

ok "服务文件已生成：$UNIT_FILE"
$SYSTEMCTL daemon-reload
$SYSTEMCTL enable "$SERVICE_NAME" >/dev/null 2>&1 || warn "设置开机自启失败（不影响本次启动）"
[ "$USER_MODE" = true ] && loginctl enable-linger "$USER" 2>/dev/null || true
ok "已设置为开机自启"

# ============================================================
# 步骤 6：放行防火墙端口
# ============================================================
step "步骤 6/7  放行防火墙端口 $PORT"

FW_DONE=false
if [ "$USER_MODE" = false ]; then
    if command -v ufw >/dev/null 2>&1 && $SUDO ufw status 2>/dev/null | grep -q "Status: active"; then
        $SUDO ufw allow "$PORT/tcp" >/dev/null 2>&1 && { ok "ufw 已放行 $PORT/tcp"; FW_DONE=true; }
    fi
    if command -v firewall-cmd >/dev/null 2>&1 && $SUDO firewall-cmd --state >/dev/null 2>&1; then
        $SUDO firewall-cmd --permanent --add-port="$PORT/tcp" >/dev/null 2>&1
        $SUDO firewall-cmd --reload >/dev/null 2>&1 && { ok "firewalld 已放行 $PORT/tcp"; FW_DONE=true; }
    fi
fi
if [ "$FW_DONE" = false ]; then
    warn "未检测到活跃的防火墙，已跳过（服务器本地通常无需额外配置）"
fi
warn "若使用云服务器（阿里云/腾讯云等），请到控制台「安全组」放行 $PORT 端口"

# ============================================================
# 步骤 7：启动服务并健康检查
# ============================================================
step "步骤 7/7  启动服务并做健康检查"

$SYSTEMCTL stop "$SERVICE_NAME" >/dev/null 2>&1 || true
sleep 1
$SYSTEMCTL start "$SERVICE_NAME" || { err "服务启动命令失败"; exit 1; }
sleep 3

if $SYSTEMCTL is-active --quiet "$SERVICE_NAME"; then
    ok "服务已启动"
else
    err "服务启动失败，最近日志如下："
    if [ "$USER_MODE" = true ]; then
        journalctl --user -u "$SERVICE_NAME" --no-pager -n 25 2>/dev/null || true
    else
        $SUDO journalctl -u "$SERVICE_NAME" --no-pager -n 25 2>/dev/null || true
    fi
    exit 1
fi

HEALTH_OK=false
for i in $(seq 1 8); do
    if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
        HEALTH_OK=true
        break
    fi
    info "等待服务就绪…（$i/8）"
    sleep 2
done
if [ "$HEALTH_OK" = true ]; then
    ok "健康检查通过"
else
    warn "健康检查未通过，请查看日志排查"
fi

# ============================================================
# 部署完成
# ============================================================
SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo ""
echo -e "${GREEN}=================================================="
echo -e "              部署完成，服务正在运行"
echo -e "==================================================${NC}"
echo ""
echo -e "  ${CYAN}【连接信息】${NC}"
echo -e "    服务器地址： http://${SERVER_IP:-你的服务器IP}:$PORT"
echo -e "    连接 Token： ${YELLOW}${TOKEN}${NC}"
echo ""
echo -e "  ${CYAN}【下一步】${NC}"
echo -e "    1. 打开 Windows 控制台，点右上角「服务器设置」"
echo -e "    2. 选择「连接远程服务器」，填入上面的地址与 Token"
echo -e "    3. 点「测试连接」确认，保存后到「账号管理」扫码登录"
echo ""
echo -e "  ${CYAN}【常用命令】${NC}"
if [ "$USER_MODE" = true ]; then
    echo -e "    查看状态： systemctl --user status $SERVICE_NAME"
    echo -e "    重启服务： systemctl --user restart $SERVICE_NAME"
    echo -e "    实时日志： journalctl --user -u $SERVICE_NAME -f"
else
    echo -e "    查看状态： systemctl status $SERVICE_NAME"
    echo -e "    重启服务： systemctl restart $SERVICE_NAME"
    echo -e "    实时日志： journalctl -u $SERVICE_NAME -f"
fi
echo -e "    查看 Token： cat $SRC_DIR/config.json"
echo ""
echo -e "  ${YELLOW}提示：Token 是连接凭证，请勿泄露给他人${NC}"
echo -e "  ${YELLOW}提示：云服务器请确认安全组已放行 $PORT 端口${NC}"
echo ""