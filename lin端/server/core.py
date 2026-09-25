"""服务端核心：配置、数据存储、日志广播与定时调度。

所有数据都落在「单文件夹」内，首次启动自动生成 config.json 与数据目录，
无需 root 权限（依赖如 Playwright 浏览器安装在用户目录）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

BASE_DIR = Path(os.environ.get("DOUYIN_FIRE_HOME", Path(__file__).resolve().parent.parent))
CONFIG_FILE = BASE_DIR / "config.json"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")

# 内置常用抖音原生表情定义（发送 douyin_sticker 消息时写入任务配置，通过核心校验）
DEFAULT_STICKERS: dict[str, dict[str, Any]] = {
    "比心": {"accessible_name": "比心", "fallback_index": 3},
    "开心": {"accessible_name": "开心", "fallback_index": 5},
    "抱抱": {"accessible_name": "抱抱", "fallback_index": 9},
    "亲亲": {"accessible_name": "亲亲", "fallback_index": 11},
    "加油": {"accessible_name": "加油", "fallback_index": 15},
    "晚安": {"accessible_name": "晚安", "fallback_index": 18},
}

LOGGER = logging.getLogger("douyin_server")

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_ID_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|]')


# ---------------------------------------------------------------- 工具


def mask_token(token: str) -> str:
    """把 Token 脱敏后用于日志输出：只显示前 4 位和后 4 位，中间用 *** 代替。"""
    if not token:
        return "(空)"
    if len(token) <= 8:
        return "*" * len(token)
    return f"{token[:4]}***{token[-4:]}"


# ---------------------------------------------------------------- 配置


def _default_config() -> dict[str, Any]:
    return {
        "token": secrets.token_hex(16),   # 首次启动自动生成，用 --token 可覆盖
        "host": "0.0.0.0",
        "port": 8765,
        "data_dir": "data",
        "scheduler_enabled": True,
        # CORS 收紧：本服务仅由桌面客户端（非浏览器）调用，跨域无意义；
        # 默认只放行本机来源，避免公网浏览器页面直接调用 API。
        "cors_origins": ["http://127.0.0.1", "http://localhost"],
        "accounts": [],
    }


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"配置文件不是有效 JSON: {path}") from exc


def write_json(path: Path, data: Any, retries: int = 5) -> None:
    """原子写入 JSON：先写临时文件（含 fsync）再 rename，保证掉电不丢数据。

    - 写满后 flush + fsync，避免 rename 先于数据块落盘导致文件为空/半写；
    - POSIX 下再对目录做一次 best-effort fsync，保证 rename 元数据也持久化；
    - Windows 下文件被占用时重试。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            tmp.replace(path)
            _fsync_dir(path.parent)
            return
        except (OSError, PermissionError) as exc:
            last_error = exc
            time.sleep(0.1 * (attempt + 1))
    if last_error:
        raise last_error


def _fsync_dir(directory: Path) -> None:
    """Best-effort 目录 fsync：让 rename 产生的元数据也落盘（仅 POSIX）。"""
    if os.name != "posix":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def load_config(argv_token: str | None = None) -> dict[str, Any]:
    """加载配置；不存在则生成默认配置。--token 参数可覆盖/初始化 token。"""
    if not CONFIG_FILE.exists():
        cfg = _default_config()
        write_json(CONFIG_FILE, cfg)
        LOGGER.info("已生成默认配置: %s", CONFIG_FILE)
    cfg = read_json(CONFIG_FILE, {})
    if not isinstance(cfg, dict):
        raise RuntimeError("config.json 必须是 JSON 对象")
    # Token 缺失或为空会让 token_guard 直接放行（鉴权形同虚设），
    # 这里检测到空 token 时自动生成并立即持久化，防止带空 token 的配置被误发布。
    if not cfg.get("token"):
        cfg["token"] = secrets.token_hex(16)
        write_json(CONFIG_FILE, cfg)
    cfg.setdefault("host", "0.0.0.0")
    cfg.setdefault("port", 8765)
    cfg.setdefault("data_dir", "data")
    cfg.setdefault("scheduler_enabled", True)
    cfg.setdefault("cors_origins", ["http://127.0.0.1", "http://localhost"])
    cfg.setdefault("accounts", [])
    if argv_token:
        cfg["token"] = argv_token
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    write_json(CONFIG_FILE, cfg)


def data_dir_of(cfg: dict[str, Any]) -> Path:
    path = Path(str(cfg.get("data_dir", "data")))
    return path if path.is_absolute() else BASE_DIR / path


def ensure_data_dirs(cfg: dict[str, Any]) -> Path:
    base = data_dir_of(cfg)
    for sub in ("storage_state", "qr", "artifacts", "tasks", "logs"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    return base


def resolve_path(base: Path, value: str) -> Path:
    """把配置中的路径解析为绝对路径（相对路径以数据目录为基准）。"""
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


# ---------------------------------------------------------------- 账号与任务


def validate_account(payload: dict[str, Any]) -> dict[str, Any]:
    """校验并归一化账号数据（唯一性检查由 API 层基于现有列表完成）。"""
    account_id = str(payload.get("id", "")).strip()
    if not account_id:
        raise ValueError("账号 ID 不能为空")
    if len(account_id) > 64:
        raise ValueError("账号 ID 不能超过 64 个字符")
    if _ID_ILLEGAL_CHARS.search(account_id):
        raise ValueError("账号 ID 不能包含文件名非法字符（\\ / : * ? \" < > |）")

    task = payload.get("task") or {}
    friends = [str(f).strip() for f in task.get("friends", []) if str(f).strip()]
    if not friends:
        raise ValueError("任务至少需要一个好友")
    if len(friends) > 100:
        raise ValueError("好友数量不能超过 100 个")

    messages = task.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("任务至少需要一条发送消息")
    if len(messages) > 50:
        raise ValueError("消息数量不能超过 50 条")
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            raise ValueError(f"第 {idx + 1} 条消息必须是对象")
        msg_type = msg.get("type")
        if msg_type not in ("text", "image", "douyin_sticker", "sticker", "random"):
            raise ValueError(f"第 {idx + 1} 条消息类型不支持: {msg_type!r}")
        if msg_type == "text" and not str(msg.get("content", "")).strip():
            raise ValueError(f"第 {idx + 1} 条文字消息内容不能为空")

    credential_type = payload.get("credential_type", "storage")
    if credential_type not in ("storage", "cookie"):
        raise ValueError("credential_type 只能是 storage 或 cookie")

    schedule_raw = payload.get("schedule", [])
    if not isinstance(schedule_raw, list):
        raise ValueError("schedule 必须是时间字符串数组")
    normalized_schedule: list[str] = []
    for t in schedule_raw:
        ts = str(t).strip()
        if not ts:
            continue
        if not _TIME_RE.match(ts):
            raise ValueError(f"调度时间格式错误（应为 HH:MM）: {ts!r}")
        normalized_schedule.append(ts)

    interval_min = float(task.get("interval_min", 3.0))
    interval_max = float(task.get("interval_max", 8.0))
    if interval_min < 0:
        raise ValueError("发送间隔最小值不能为负数")
    if interval_max < interval_min:
        raise ValueError("发送间隔最大值不能小于最小值")
    # 拟人下限：单条消息间隔不低于 3 秒，防止配置过小导致高频连发（风控高危）
    interval_min = max(3.0, interval_min)
    interval_max = max(interval_min, interval_max)

    daily_max_sends = task.get("daily_max_sends", 20)
    if isinstance(daily_max_sends, bool) or not isinstance(daily_max_sends, int) or daily_max_sends < 0:
        raise ValueError("daily_max_sends 必须是非负整数")

    return {
        "id": account_id,
        "group": str(payload.get("group", "默认")).strip() or "默认",
        "enabled": bool(payload.get("enabled", True)),
        "credential_type": credential_type,
        "credential": str(payload.get("credential", "")).strip(),
        "cookie_raw": str(payload.get("cookie_raw", "")).strip(),
        "headless": bool(payload.get("headless", True)),
        "schedule": sorted(set(normalized_schedule)),
        "task": {
            "friends": friends,
            "messages": list(messages),
            "interval_min": interval_min,
            "interval_max": interval_max,
            "prevent_duplicates": bool(task.get("prevent_duplicates", True)),
            "continue_on_error": bool(task.get("continue_on_error", True)),
            "daily_max_sends": daily_max_sends,
            "skip_if_today_outgoing": bool(task.get("skip_if_today_outgoing", True)),
        },
    }


def collect_stickers(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """收集消息中用到的原生表情，生成任务配置的 stickers 段。"""
    used: set[str] = set()

    def visit(message: Any) -> None:
        if isinstance(message, dict):
            if message.get("type") == "douyin_sticker" and message.get("sticker"):
                used.add(str(message["sticker"]))
            for choice in message.get("choices", []) or []:
                visit(choice)

    for message in messages:
        visit(message)
    return {name: DEFAULT_STICKERS.get(name, {"accessible_name": name}) for name in sorted(used)}


def write_task_config(data_dir: Path, account: dict[str, Any], force: bool = False) -> Path:
    """把账号的任务配置落盘为核心引擎可读的 tasks/<id>.json。

    force=True（手动运行/补发）时临时关闭当日防重复，保证消息真实发送；
    自动调度保持防重复，避免同一天重复打扰好友。
    """
    task = account["task"]
    path = data_dir / "tasks" / f"{account['id']}.json"
    payload = {
        "timezone": "Asia/Shanghai",
        "task_id": f"{account['id']}-streak",
        "targets": [
            {"name": name, "messages": list(task["messages"])}
            for name in task["friends"]
        ],
        "send_interval_seconds": {"min": task["interval_min"], "max": task["interval_max"]},
        "continue_on_error": task["continue_on_error"],
        "prevent_duplicates": False if force else task["prevent_duplicates"],
        "target_open_retries": 1,
        "target_open_timeout_seconds": 15,
        "daily_max_sends": task.get("daily_max_sends", 20),
        "skip_if_today_outgoing": task.get("skip_if_today_outgoing", True),
        "stickers": collect_stickers(task["messages"]),
    }
    write_json(path, payload)
    return path


# ---------------------------------------------------------------- 运行历史


def load_runs(data_dir: Path) -> list[dict[str, Any]]:
    """加载运行记录；文件损坏时自动备份并重置，避免服务瘫痪。"""
    path = data_dir / "runs.json"
    try:
        raw = read_json(path, {})
    except RuntimeError:
        backup = path.with_suffix(".json.bak")
        try:
            path.rename(backup)
            LOGGER.warning("runs.json 损坏，已备份到 %s 并重置", backup)
        except Exception:
            LOGGER.exception("runs.json 损坏且备份失败，强制重置")
        return []
    runs = raw.get("runs", []) if isinstance(raw, dict) else []
    return runs if isinstance(runs, list) else []


def save_run(data_dir: Path, runs: list[dict[str, Any]], record: dict[str, Any]) -> None:
    runs.append(record)
    write_json(data_dir / "runs.json", {"runs": runs[-2000:]})


# ---------------------------------------------------------------- 日志广播


class BroadcastHandler(logging.Handler):
    """日志广播 handler：把每条日志推送给注册的 sink（WebSocket 等）。"""

    _keep = True  # 核心 _configure_logging(reset=True) 时保留外部 handler

    def __init__(self, sink: Callable[[str], None]) -> None:
        super().__init__()
        self.sink = sink
        self.addFilter(UvicornAccessFilter())  # 不广播 uvicorn 的每请求访问日志，避免客户端日志刷屏

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink(self.format(record))
        except Exception:
            pass


class UvicornAccessFilter(logging.Filter):
    """过滤 uvicorn 访问日志（"GET /api/... 200" 每请求一行），文件与广播均跳过。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name != "uvicorn.access"


def read_log_tail(data_dir: Path, limit: int = 200) -> list[str]:
    """从 server.log 尾部读取最近日志行（服务重启/长时间运行后仍可同步）。"""
    log_file = data_dir / "logs" / "server.log"
    try:
        if not log_file.is_file():
            return []
        with log_file.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        cleaned = [ln.rstrip("\r\n") for ln in lines if ln.strip()]
        return cleaned[-limit:]
    except OSError:
        return []


# ---------------------------------------------------------------- 定时调度


class Scheduler:
    """每天按账号配置的时间点触发任务；到点后随机抖动 0-30 分钟执行。

    抖动幅度足以覆盖真人"不一定每天准点"的规律（固定准点是机器特征）；
    使用北京时间（Asia/Shanghai），与服务器本地时区无关（tzdata 不可用时回退本地时间）。
    已触发记录持久化到 data/scheduler_state.json，服务重启后不重复执行；
    每天自动清理昨天的记录，避免状态文件无限增长。
    """

    _JITTER_RANGE_SECONDS = 30 * 60
    _SCHEDULER_TZ = "Asia/Shanghai"

    def __init__(self, cfg: dict[str, Any], data_dir: Path, runner: Any) -> None:
        self.cfg = cfg
        self.data_dir = data_dir
        self.runner = runner
        self._state_file = data_dir / "scheduler_state.json"
        self._fired: dict[str, dict[str, str]] = self._load_state()  # {account_id: {time: date}}
        self._task: asyncio.Task | None = None
        self._pending: set[asyncio.Task] = set()  # 抖动等待中的触发任务，stop 时统一取消

    def _load_state(self) -> dict[str, dict[str, str]]:
        """从磁盘加载已触发记录；损坏或不存在时返回空。"""
        try:
            raw = read_json(self._state_file, {})
            if isinstance(raw, dict):
                return {k: v for k, v in raw.items() if isinstance(v, dict)}
        except RuntimeError:
            LOGGER.warning("scheduler_state.json 损坏，已重置")
        return {}

    async def _save_state(self) -> None:
        try:
            await asyncio.to_thread(self._save_state_sync)
        except OSError:
            LOGGER.warning("无法保存调度器状态到 %s", self._state_file)

    def _save_state_sync(self) -> None:
        try:
            write_json(self._state_file, self._fired)
        except OSError:
            raise

    @staticmethod
    def _now_shanghai() -> tuple[str, str]:
        """返回 (今天日期, 当前时间 HH:MM)，按 Asia/Shanghai 时区；tzdata 不可用时回退本地时间。"""
        try:
            now = datetime.now(ZoneInfo(Scheduler._SCHEDULER_TZ))
        except Exception:
            now = datetime.now()
        return now.date().isoformat(), now.strftime("%H:%M")

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        # 取消主循环 + 所有待执行的抖动任务，避免停机时 pending 任务被中断丢失
        if self._task is not None and not self._task.done():
            self._task.cancel()
        for pending in list(self._pending):
            pending.cancel()
        self._pending.clear()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("调度器 tick 异常")
            await asyncio.sleep(30)

    async def tick(self) -> None:
        if not self.cfg.get("scheduler_enabled", True):
            return
        today, now = self._now_shanghai()
        # 清理昨天及更早的已触发记录（只保留今天），避免状态文件无限增长
        changed = False
        for account_fired in self._fired.values():
            stale = [t for t, d in account_fired.items() if d != today]
            for t in stale:
                del account_fired[t]
                changed = True
        if changed:
            await self._save_state()

        for account in self.cfg.get("accounts", []):
            if not account.get("enabled"):
                continue
            for target in sorted(set(account.get("schedule", []))):
                if target > now:
                    continue
                fired = self._fired.setdefault(account["id"], {})
                if fired.get(target) == today:
                    continue
                fired[target] = today
                await self._save_state()
                jitter = random.randint(0, self._JITTER_RANGE_SECONDS)
                pending = asyncio.create_task(self._delayed_run(account, jitter, target))
                self._pending.add(pending)
                pending.add_done_callback(self._pending.discard)
                LOGGER.info("[%s] 计划触发 %s，抖动 %d 秒后执行", account["id"], target, jitter)
                break  # 每个账号每次 tick 最多触发一个时间点

    async def _delayed_run(self, account: dict[str, Any], delay_seconds: int, target_time: str) -> None:
        await asyncio.sleep(delay_seconds)
        # 抖动等待后重新检查：调度可能被关闭、账号可能被删除/禁用
        if not self.cfg.get("scheduler_enabled", True):
            LOGGER.info("[%s] 调度已关闭，跳过计划触发 %s", account["id"], target_time)
            return
        current = next((a for a in self.cfg.get("accounts", []) if a.get("id") == account["id"]), None)
        if current is None:
            LOGGER.info("[%s] 账号已删除，跳过计划触发 %s", account["id"], target_time)
            return
        if not current.get("enabled"):
            LOGGER.info("[%s] 账号已禁用，跳过计划触发 %s", account["id"], target_time)
            return
        try:
            await self.runner.run_account(current)
        except Exception:
            LOGGER.exception("[%s] 定时任务执行异常", account["id"])
