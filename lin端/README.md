# 抖音自动续火花 · Linux 自动化服务端

在 Linux 服务器上跑抖音私信发送任务，通过 HTTP REST API + WebSocket
和 Windows 客户端通信。单个文件夹，上传后一条命令跑起来，默认不需要 root。

账号风险、合规责任和部署安全条款写在包根目录的 `免责声明.txt`，部署前先过一遍。

## 特性

- Playwright 无头浏览器执行发送（复用原项目核心引擎，含反自动化指纹）
- 完整 API：扫码登录（返回二维码）/ 账号 CRUD / 任务运行 / 状态查询 / 日志
- WebSocket 实时日志推送（`/ws/logs?token=xxx`）
- 内置定时调度：按账号配置的时间点自动发送（带随机抖动，降低风控概率）
- 配置外置 `config.json`，首次启动自动生成（Token 随机生成）
- Token 鉴权保护全部接口

## 一键部署

把整个文件夹拖到服务器，执行一条命令搞定全部：

```bash
# 系统级（推荐，需要 sudo）
sudo bash deploy.sh

# 或用户级（无需 root）
bash deploy.sh --user
```

`deploy.sh` 自动完成 7 步：
1. 检测系统 + 安装系统依赖（python3/venv/pip/Chromium 运行库）
2. 创建 venv + 安装 pip 依赖
3. 安装 Playwright Chromium 浏览器
4. 生成 config.json（自动生成 Token）
5. 注册 systemd 服务（开机自启 + 崩溃自动重启）
6. 配置防火墙（放行端口）
7. 启动服务 + 健康检查 + 输出连接信息

部署完成后直接显示：内网/公网地址、端口、API Token、Win 端配置方法、服务管理命令。

## 卸载

```bash
sudo bash uninstall.sh        # 系统级
bash uninstall.sh --user      # 用户级
```

## 手动部署

如果不想用 deploy.sh，也可以分两步手动部署：

```bash
bash start.sh                 # 前台运行（测试用）
bash start.sh --daemon        # 后台运行
sudo bash install-service.sh  # 注册开机自启
```

## 服务管理命令

```bash
systemctl status douyin-fire      # 查看状态
systemctl restart douyin-fire     # 重启
systemctl stop douyin-fire        # 停止
journalctl -u douyin-fire -f      # 实时日志
cat config.json                    # 查看 Token
```

## 目录结构

```text
lin端/
├── main.py             # 服务入口（--port / --token 可覆盖配置）
├── server/
│   ├── core.py         # 配置/存储/日志广播/定时调度
│   ├── runner.py       # 任务执行器（复用核心发送引擎）
│   ├── login.py        # 无头扫码登录（二维码截图 + 自动检测）
│   └── api.py          # FastAPI 路由 + Token 鉴权 + WebSocket
├── app/                # 原项目核心引擎（Playwright 发送逻辑）
├── config.json         # 首次启动自动生成（Token/端口/账号）
├── data/               # 数据目录（自动创建）
│   ├── storage_state/  # 各账号登录态
│   ├── tasks/          # 任务配置（自动生成）
│   ├── artifacts/      # 运行产物（日志/截图/结果）
│   ├── qr/             # 登录二维码图片
│   └── logs/           # 服务端日志
├── start.sh            # 一键启动
├── install-service.sh  # systemd 自启安装
└── requirements.txt
```

## API 一览（除 /api/health 外均需 Token）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/accounts` | 账号列表 |
| POST | `/api/accounts` | 新增账号 |
| PUT | `/api/accounts/{id}` | 更新账号 |
| DELETE | `/api/accounts/{id}` | 删除账号 |
| GET | `/api/accounts/{id}/status` | 账号状态/最近运行 |
| POST | `/api/accounts/{id}/run` | 立即运行（body: `{"dry_run": bool}`） |
| POST | `/api/login/qr` | 发起扫码登录（body: `{"account_id": "..."}`） |
| GET | `/api/login/qr/{sid}/image` | 二维码图片（PNG） |
| GET | `/api/login/qr/{sid}/status` | 登录状态 |
| GET | `/api/logs` | 最近日志 |
| GET | `/api/runs` | 最近运行记录 |
| GET/PUT | `/api/schedule` | 查看/开关自动调度 |
| WS | `/ws/logs?token=xxx` | 实时日志推送 |

鉴权方式：`Authorization: Bearer <token>`（WebSocket 用 `?token=`）。

## 账号配置（config.json 中的 accounts）

```json
{
  "id": "小号A",
  "group": "默认",
  "enabled": true,
  "credential_type": "storage",
  "credential": "storage_state/小号A.json",
  "headless": true,
  "schedule": ["09:00", "21:00"],
  "task": {
    "friends": ["好友A", "好友B"],
    "messages": [
      {"type": "text", "content": "今天也要开心"},
      {"type": "random", "choices": [{"type": "text", "content": "早安"}, {"type": "text", "content": "午安"}]}
    ],
    "interval_min": 3,
    "interval_max": 8
  }
}
```

- `credential_type=storage`：登录态文件（推荐，扫码登录产物）；
  `credential_type=cookie`：Cookie 文件路径或直接粘贴 `cookie_raw` JSON。
- `schedule` 为空则仅手动触发；`enabled=false` 跳过调度与手动运行。
- 消息类型：`text` / `image`（path 为服务器上的绝对路径）/
  `douyin_sticker` / `random`（choices 随机选一）。

## 传输安全

服务端默认用明文 HTTP/WebSocket 提供 API，Token 和账号数据在网络上不加密。
下面三种方式挑一种，别把 8765 直接裸在公网：

1. SSH 隧道（最简单）：客户端本机执行 `ssh -L 28888:127.0.0.1:8765 root@服务器IP -N`，客户端地址填 `127.0.0.1:28888`，全程走 SSH 加密，服务器不用放行 8765；
2. nginx 反代 + HTTPS/WSS：把 8765 反代到 443 并配证书，客户端地址改成 `https://域名`（`api_client.py` 按前缀自动切 `https://` 和 `wss://`）；
3. 若必须直连公网，至少用云防火墙把 8765 端口限制到可信来源 IP。

登录态文件（`data/storage_state/`）和 `config.json` 都是敏感凭证。登录态文件保存时会设成仅所有者可读（POSIX `0600`），所在目录权限是 `700`；`config.json` 没做额外权限处理，沿用系统 umask，部署时自己看一眼别是全局可读。这两个别提交进版本库，也别随手发给别人。

## 注意

- 服务器出口 IP 可能触发抖音安全验证：扫码登录若失败，
  可在本机浏览器导出 Cookie（Cookie-Editor）后通过账号 API 直接导入。
- 人机验证：服务器没有显示器，没法在浏览器里操作滑块或点选验证码；
  登录触发验证码时改用 Cookie 导入。
  若服务器配置了显示器或 Xvfb，可在客户端登录请求中传 `"headless": false`
  让浏览器有头运行，通过 VNC 等方式操作验证码。
- 首次安装 Chromium 约需 200MB 磁盘。非 root 用户直接跑 `playwright install chromium`，
  浏览器会装到用户目录，不需要 sudo。
- 如需更换端口/Token：`python main.py --port 8766 --token xxxx` 或直接改 config.json 后重启。

## 配置项说明（config.json）

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `token` | string | 自动生成 | API 鉴权令牌，首次启动随机生成 32 位十六进制 |
| `host` | string | `0.0.0.0` | 监听地址，`0.0.0.0` 表示所有网卡 |
| `port` | number | `8765` | 监听端口 |
| `data_dir` | string | `data` | 数据目录（相对项目根目录） |
| `scheduler_enabled` | bool | `true` | 是否启用定时调度 |
| `cors_origins` | string[] | 本机来源 | 允许跨域来源；默认仅本机（`http://127.0.0.1`、`http://localhost`），桌面客户端不受影响 |
| `accounts` | array | `[]` | 账号配置列表 |

限制跨域来源（可选）：在 config.json 里加：
```json
"cors_origins": ["http://localhost:3000", "https://your-domain.com"]
```

## 常见问题

Q: 服务启动后浏览器连不上？
A: 按顺序查三步：① `systemctl status douyin-fire` 看服务在不在跑；② 云服务器安全组有没有放行端口；③ 本地防火墙 `sudo ufw status` 或 `sudo firewall-cmd --list-ports`。

Q: 提示"端口已被占用"？
A: 换个端口：`python main.py --port 8766`，或改 config.json 的 `port` 字段后重启。

Q: config.json 损坏，服务起不来？
A: 删掉 config.json，重启会自动生成新的（Token 会变，客户端要同步更新）。也可以手动把 JSON 修好。

Q: 扫码登录超时？
A: 服务器出口 IP 可能触发了抖音安全验证。改用 Cookie：在本机浏览器用 Cookie-Editor 导出 JSON，通过账号 API 的 `cookie_raw` 字段导入。

Q: 任务运行后提示"登录态文件不存在"？
A: 账号还没配登录凭证。先调 `/api/login/qr` 扫码登录，或在账号配置里设 `credential_type=cookie` 并导入 Cookie。

Q: 怎么看服务日志？
A: 实时日志 `journalctl -u douyin-fire -f`；文件日志在 `data/logs/server.log`；也可以走 WebSocket `/ws/logs?token=xxx`。

Q: 定时任务不准时？
A: 调度固定用 Asia/Shanghai 时区，每 30 秒检查一次，到点后有 0-30 分钟随机抖动（降低风控识别）。服务器时区不是上海也没影响，代码内部已强制用上海时间。

Q: 服务崩溃重启后，当天的任务会重复执行吗？
A: 不会。调度器的已触发记录存在 `data/scheduler_state.json`，重启后读回来，当天不会重复触发。

Q: 之前崩溃过，现在启动提示"已有任务正在运行"？
A: 锁文件发现旧进程 PID 已经不存在时会自动清理。还不行就手动删掉 `data/artifacts/<账号ID>/run.lock`。
