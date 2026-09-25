"""Win 端网络层：HTTP API 客户端 + WebSocket 实时日志监听。

所有数据均来自 Linux 服务端；本文件不含任何自动化逻辑。
"""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import requests
import websockets
from PySide6.QtCore import QThread, Signal


class ApiError(Exception):
    """服务端不可达、Token 无效或接口返回错误的统一异常。"""


class ApiClient:
    """HTTP 客户端：连接配置持久化在客户端本地 client_config.json。"""

    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config_path = Path(config_path) if config_path else Path(__file__).resolve().parent / "config.json"
        self.config = self._load()
        self._session = requests.Session()

    # ------------------------------------------------------------- 配置

    def _load(self) -> dict:
        default = {"mode": "local", "server": {"host": "127.0.0.1", "port": 18765, "token": ""}}
        try:
            saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            saved = {}
        server = {**default["server"], **saved.get("server", {})}
        return {"mode": saved.get("mode", "local"), "server": server}

    def save_config(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写入：先写临时文件再替换，避免中途崩溃留下半写的 client_config.json
        temporary = self.config_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.config_path)

    def set_mode(self, mode: str) -> None:
        self.config["mode"] = mode
        self.save_config()

    def update_server(self, host: str, port: int, token: str) -> None:
        self.config["server"].update(host=host.strip(), port=int(port), token=token.strip())
        self.save_config()

    @property
    def base_url(self) -> str:
        server = self.config["server"]
        return f"http://{server['host']}:{server['port']}"

    @property
    def ws_url(self) -> str:
        server = self.config["server"]
        return f"ws://{server['host']}:{server['port']}/ws/logs?token={server['token']}"

    # ------------------------------------------------------------- HTTP

    def request(self, method: str, path: str, *, auth: bool = True, json_body=None, timeout: float = 10.0):
        headers = {"Content-Type": "application/json"}
        if auth and self.config["server"]["token"]:
            headers["Authorization"] = f"Bearer {self.config['server']['token']}"
        try:
            response = self._session.request(
                method, self.base_url + path, headers=headers, json=json_body, timeout=timeout
            )
        except requests.RequestException as exc:
            raise ApiError(f"无法连接服务器（{exc}）") from exc
        if response.status_code == 401:
            raise ApiError("Token 无效或未授权，请检查设置")
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise ApiError(str(detail))
        return response.json() if response.content else None

    # ------------------------------------------------------------- API

    def health(self) -> dict:
        return self.request("GET", "/api/health", auth=False, timeout=5)

    def list_accounts(self) -> list[dict]:
        return self.request("GET", "/api/accounts").get("accounts", [])

    def create_account(self, payload: dict) -> dict:
        return self.request("POST", "/api/accounts", json_body=payload)

    def update_account(self, account_id: str, payload: dict) -> dict:
        from urllib.parse import quote

        return self.request("PUT", f"/api/accounts/{quote(account_id, safe='')}", json_body=payload)

    def delete_account(self, account_id: str) -> None:
        from urllib.parse import quote

        self.request("DELETE", f"/api/accounts/{quote(account_id, safe='')}")

    def account_status(self, account_id: str) -> dict:
        from urllib.parse import quote

        return self.request("GET", f"/api/accounts/{quote(account_id, safe='')}/status").get("status", {})

    def run_account(self, account_id: str, dry_run: bool = False, force: bool = False, timeout: float = 600.0) -> dict:
        from urllib.parse import quote

        return self.request(
            "POST", f"/api/accounts/{quote(account_id, safe='')}/run",
            json_body={"dry_run": dry_run, "force": force}, timeout=timeout,
        )

    def start_login(self, account_id: str, headless: bool = True) -> dict:
        """发起扫码登录；headless=False 时服务端会弹出可见浏览器窗口（可操作人机验证）。"""
        return self.request("POST", "/api/login/qr", json_body={"account_id": account_id, "headless": headless})

    def login_status(self, session_id: str) -> dict:
        return self.request("GET", f"/api/login/qr/{session_id}/status")

    def login_image(self, session_id: str) -> bytes:
        """拉取二维码图片原始字节（带鉴权）。"""
        headers = {}
        if self.config["server"]["token"]:
            headers["Authorization"] = f"Bearer {self.config['server']['token']}"
        try:
            response = self._session.get(
                self.base_url + f"/api/login/qr/{session_id}/image",
                headers=headers, timeout=15,
            )
        except requests.RequestException as exc:
            raise ApiError(f"获取二维码失败（{exc}）") from exc
        if response.status_code == 401:
            raise ApiError("Token 无效或未授权，请检查设置")
        if response.status_code != 200:
            raise ApiError(f"获取二维码失败（HTTP {response.status_code}）")
        return response.content

    def get_schedule(self) -> dict:
        return self.request("GET", "/api/schedule")

    def set_schedule(self, enabled: bool) -> dict:
        return self.request("PUT", "/api/schedule", json_body={"enabled": enabled})

    def get_logs(self, limit: int = 200) -> list[str]:
        return self.request("GET", f"/api/logs?limit={limit}").get("logs", [])

    def get_runs(self, limit: int = 50) -> list[dict]:
        """最近运行记录。"""
        return self.request("GET", f"/api/runs?limit={limit}").get("runs", [])

    def sync_logs_to_local(self, output_path: str | Path, limit: int = 500) -> int:
        """拉取服务器日志并保存到本地文件，返回写入行数。

        Win 端每隔几天上线时调用，把服务器期间的运行日志同步到本地查看。
        """
        from pathlib import Path as _Path

        lines = self.get_logs(limit=limit)
        out = _Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines), encoding="utf-8")
        return len(lines)


class LogListener(QThread):
    """WebSocket 日志监听：断线自动重连（3 秒间隔）。"""

    line = Signal(str)
    connected = Signal(bool)   # True 连接成功 / False 断开或失败

    def __init__(self, api: ApiClient, parent=None) -> None:
        super().__init__(parent)
        self.api = api
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        import time

        while not self._stop:
            try:
                asyncio.run(self._listen())
            except Exception:
                self.connected.emit(False)
            if not self._stop:
                # 断开后等待 3 秒自动重连
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not self._stop:
                    time.sleep(0.1)

    async def _listen(self) -> None:
        async with websockets.connect(self.api.ws_url, open_timeout=5) as websocket:
            self.connected.emit(True)
            while not self._stop:
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue  # 每秒检查一次停止标志，保证能及时退出
                self.line.emit(str(message))
