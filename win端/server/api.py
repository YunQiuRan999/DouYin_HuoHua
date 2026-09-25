"""HTTP API 与 WebSocket 日志推送。

- 除 /api/health 外全部要求 Token（Authorization: Bearer <token> 或 ?token=）；
- WebSocket 日志路径 /ws/logs?token=xxx 实时推送任务日志；
- 账号 CRUD、扫码登录、任务运行、状态查询、调度开关、运行记录。
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import shutil
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from server import core

LOGGER = logging.getLogger("douyin_server")


# ---------------------------------------------------------------- 日志广播


class LogManager:
    """维护 WebSocket 客户端集合，并缓存最近日志供新客户端补发。"""

    def __init__(self, max_buffer: int = 300) -> None:
        self._clients: set[WebSocket] = set()
        self._buffer: deque[str] = deque(maxlen=max_buffer)

    def recent(self, limit: int = 200) -> list[str]:
        return list(self._buffer)[-limit:]

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)
        for line in self._buffer:
            await websocket.send_text(line)

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)

    def broadcast(self, line: str) -> None:
        self._buffer.append(line)
        for websocket in list(self._clients):
            asyncio.create_task(self._send(websocket, line))

    async def _send(self, websocket: WebSocket, line: str) -> None:
        try:
            await websocket.send_text(line)
        except Exception:
            self._clients.discard(websocket)


# ---------------------------------------------------------------- 鉴权


def token_guard(cfg: dict[str, Any]):
    async def guard(authorization: str = Header(""), token: str = Query("")) -> None:
        expected = str(cfg.get("token", "") or "")
        if not expected:
            return
        # 恒定时间比较，避免时序侧信道泄露 token 前缀
        bearer_ok = hmac.compare_digest(authorization, f"Bearer {expected}")
        query_ok = hmac.compare_digest(token, expected)
        if not bearer_ok and not query_ok:
            raise HTTPException(status_code=401, detail="无效的 Token，请在设置中填写正确的 Token")
    return guard


# ---------------------------------------------------------------- 请求模型


class AccountIn(BaseModel):
    id: str = Field(..., description="账号 ID")
    group: str = "默认"
    enabled: bool = True
    credential_type: str = "storage"
    credential: str = ""
    cookie_raw: str = ""
    headless: bool = True
    schedule: list[str] = []
    task: dict[str, Any] = Field(..., description="friends/messages/interval 等")


class RunIn(BaseModel):
    dry_run: bool = False
    force: bool = False


class ScheduleIn(BaseModel):
    enabled: bool


class LoginStartIn(BaseModel):
    account_id: str = Field(..., min_length=1, max_length=64, description="账号 ID")
    headless: Optional[bool] = Field(None, description="是否无头；None 使用服务端默认")


# ---------------------------------------------------------------- 应用工厂


def create_app(
    cfg: dict[str, Any],
    data_dir: Path,
    runner: Any,
    login_manager: Any,
    scheduler: Any,
    log_manager: LogManager,
) -> FastAPI:

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        scheduler.start()
        yield
        scheduler.stop()

    app = FastAPI(title="抖音自动续火花服务端", version="1.0.1", lifespan=lifespan)

    cors_origins = cfg.get("cors_origins", ["*"])
    if not isinstance(cors_origins, list) or not cors_origins:
        cors_origins = ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    guard = Depends(token_guard(cfg))

    def _find_account(account_id: str) -> dict[str, Any]:
        for account in cfg["accounts"]:
            if account["id"] == account_id:
                return account
        raise HTTPException(status_code=404, detail=f"账号不存在: {account_id}")

    # ------------------------------------------------------------ 基础

    @app.get("/api/health", tags=["基础"])
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": "1.0.1",
            "scheduler_enabled": bool(cfg.get("scheduler_enabled", True)),
            "running": runner.is_busy(),
            "accounts": len(cfg["accounts"]),
        }

    @app.get("/api/logs", tags=["基础"], dependencies=[guard])
    async def logs(limit: int = 200) -> dict[str, Any]:
        limit = min(max(limit, 1), 2000)
        # 优先读取持久化的 server.log 尾部（服务重启/长时间无人值守后，Win 端上线仍可同步历史日志）；
        # 文件不可用时回退内存缓冲（仅实时日志）。
        file_lines = core.read_log_tail(data_dir, limit)
        if file_lines:
            return {"logs": file_lines}
        return {"logs": log_manager.recent(limit)}

    @app.get("/api/runs", tags=["基础"], dependencies=[guard])
    async def recent_runs(limit: int = 50) -> dict[str, Any]:
        """最近运行记录（数据已收集但此前未暴露）。"""
        return {"runs": runner.recent_runs(limit)}

    # ------------------------------------------------------------ 账号

    @app.get("/api/accounts", tags=["账号"], dependencies=[guard])
    async def list_accounts() -> dict[str, Any]:
        accounts = []
        for account in cfg["accounts"]:
            item = dict(account)
            item["last_run"] = account.get("last_run")
            accounts.append(item)
        return {"accounts": accounts}

    @app.post("/api/accounts", tags=["账号"], dependencies=[guard])
    async def create_account(payload: AccountIn) -> dict[str, Any]:
        data = payload.model_dump()
        if any(a["id"] == data["id"] for a in cfg["accounts"]):
            raise HTTPException(status_code=400, detail=f"账号 ID 已存在: {data['id']}")
        try:
            account = core.validate_account(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        cfg["accounts"].append(account)
        core.save_config(cfg)
        LOGGER.info("新增账号: %s", account["id"])
        return {"account": account}

    @app.put("/api/accounts/{account_id}", tags=["账号"], dependencies=[guard])
    async def update_account(account_id: str, payload: AccountIn) -> dict[str, Any]:
        existing = _find_account(account_id)
        data = payload.model_dump()
        if data["id"] != account_id and any(a["id"] == data["id"] for a in cfg["accounts"]):
            raise HTTPException(status_code=400, detail=f"账号 ID 已存在: {data['id']}")
        try:
            account = core.validate_account(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        index = cfg["accounts"].index(existing)
        account["last_run"] = existing.get("last_run")
        account["cooldown_until"] = existing.get("cooldown_until")  # 更新账号不重置风控冷却
        cfg["accounts"][index] = account
        core.save_config(cfg)
        LOGGER.info("更新账号: %s", account["id"])
        return {"account": account}

    @app.delete("/api/accounts/{account_id}", tags=["账号"], dependencies=[guard])
    async def delete_account(account_id: str) -> dict[str, Any]:
        existing = _find_account(account_id)
        cfg["accounts"].remove(existing)
        core.save_config(cfg)
        # 清理该账号的持久化浏览器目录，避免残留隐私数据与磁盘堆积
        profile_dir = data_dir / "profiles" / account_id
        if profile_dir.is_dir():
            shutil.rmtree(profile_dir, ignore_errors=True)
            LOGGER.info("已清理账号浏览器数据: %s", account_id)
        LOGGER.info("删除账号: %s", account_id)
        return {"deleted": account_id}

    @app.get("/api/accounts/{account_id}/status", tags=["账号"], dependencies=[guard])
    async def account_status(account_id: str) -> dict[str, Any]:
        account = _find_account(account_id)
        return {"status": runner.account_status(account)}

    # ------------------------------------------------------------ 运行

    @app.post("/api/accounts/{account_id}/run", tags=["运行"], dependencies=[guard])
    async def run_account(account_id: str, payload: RunIn) -> dict[str, Any]:
        account = _find_account(account_id)
        if runner.is_busy():
            raise HTTPException(status_code=409, detail="已有任务正在运行，请稍候")
        LOGGER.info("手动触发账号 %s（dry_run=%s, force=%s）", account_id, payload.dry_run, payload.force)
        record = await runner.run_account(account, dry_run=payload.dry_run, force=payload.force)
        return {"record": record}

    # ------------------------------------------------------------ 扫码登录

    @app.post("/api/login/qr", tags=["登录"], dependencies=[guard])
    async def qr_login_start(payload: LoginStartIn) -> dict[str, Any]:
        account_id = payload.account_id.strip()
        if not account_id:
            raise HTTPException(status_code=400, detail="需要 account_id（登录态将保存到该 ID 名下）")
        sid = login_manager.start(account_id, headless=payload.headless)
        return {"session_id": sid}

    @app.get("/api/login/qr/{sid}/status", tags=["登录"], dependencies=[guard])
    async def qr_login_status(sid: str) -> dict[str, Any]:
        status = login_manager.status(sid)
        if status is None:
            raise HTTPException(status_code=404, detail="登录会话不存在或已过期")
        return status

    @app.get("/api/login/qr/{sid}/image", tags=["登录"], dependencies=[guard])
    async def qr_login_image(sid: str) -> FileResponse:
        path = login_manager.qr_image_path(sid)
        if path is None:
            raise HTTPException(status_code=404, detail="二维码尚未生成")
        return FileResponse(path, media_type="image/png")

    # ------------------------------------------------------------ 调度

    @app.get("/api/schedule", tags=["调度"], dependencies=[guard])
    async def schedule_status() -> dict[str, Any]:
        return {"enabled": bool(cfg.get("scheduler_enabled", True)), "running": runner.is_busy()}

    @app.put("/api/schedule", tags=["调度"], dependencies=[guard])
    async def schedule_update(payload: ScheduleIn) -> dict[str, Any]:
        cfg["scheduler_enabled"] = payload.enabled
        core.save_config(cfg)
        LOGGER.info("自动调度已%s", "开启" if payload.enabled else "关闭")
        return {"enabled": payload.enabled}

    # ------------------------------------------------------------ WebSocket

    @app.websocket("/ws/logs")
    async def ws_logs(websocket: WebSocket, token: str = Query("")) -> None:
        expected = str(cfg.get("token", "") or "")
        if expected and not hmac.compare_digest(token, expected):
            await websocket.close(code=4401, reason="无效 Token")
            return
        await log_manager.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            log_manager.disconnect(websocket)
        except Exception:
            log_manager.disconnect(websocket)

    return app
