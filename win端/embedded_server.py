"""内嵌自动化服务：在本地 127.0.0.1 启动与 Linux 端相同的 FastAPI 服务。

这样 Win 端一个 exe 即可：
- 本地模式：内嵌服务在本机直接跑 Playwright 发送（无需 Linux 服务器）；
- 远程模式：客户端 UI 切换指向外部 Linux 服务器（代码完全复用，零差异）。

数据目录：exe 同级目录（DOUYIN_FIRE_HOME），config.json 首次启动自动生成。
"""
from __future__ import annotations

import logging
import os
import secrets
import socket
import threading
import time
from pathlib import Path

import requests


def _pick_free_port(start: int = 18765, attempts: int = 40) -> int:
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("找不到可用端口（18765-18804 均被占用）")


class EmbeddedServer:
    """后台线程运行 uvicorn；启动成功（/api/health 可访问）后返回。"""

    def __init__(self, home_dir: str | Path) -> None:
        self.home = Path(home_dir)
        self.home.mkdir(parents=True, exist_ok=True)
        self.port = 0
        self.token = ""
        self._thread: threading.Thread | None = None
        self._server = None  # uvicorn.Server 实例，stop() 时优雅退出
        self._error: str = ""

    def start(self) -> None:
        # 已在运行则直接复用（本地/远程模式来回切换时避免重复启动）
        if self._thread is not None and self._thread.is_alive():
            return
        config_path = self.home / "config.json"
        if config_path.exists():
            try:
                import json

                saved = json.loads(config_path.read_text(encoding="utf-8"))
                self.port = int(saved.get("port", 0))
                self.token = str(saved.get("token", ""))
            except Exception:
                self.port = 0
        if not self.port or not self._port_free(self.port):
            self.port = _pick_free_port()
        if not self.token:
            self.token = secrets.token_hex(16)

        os.environ["DOUYIN_FIRE_HOME"] = str(self.home)
        self._thread = threading.Thread(target=self._run_server, daemon=True, name="embedded-server")
        self._thread.start()
        for _ in range(150):  # 最长等待 15 秒
            if self._error:
                raise RuntimeError(f"内嵌服务启动失败: {self._error}")
            try:
                response = requests.get(f"http://127.0.0.1:{self.port}/api/health", timeout=1)
                if response.status_code == 200:
                    return
            except requests.RequestException:
                pass
            time.sleep(0.1)
        raise RuntimeError(f"内嵌服务启动超时: {self._error or '未知错误'}")

    def stop(self) -> None:
        """优雅停止本地引擎：远程模式下释放端口与资源，避免与 SSH 隧道端口冲突。"""
        server = self._server
        if server is not None:
            server.should_exit = True
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        self._thread = None
        self._server = None

    def _port_free(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            return sock.connect_ex(("127.0.0.1", port)) != 0

    def _run_server(self) -> None:
        try:
            import uvicorn

            from server import api, core, login, runner
            from server.api import LogManager

            # 预热 Playwright：首次 import 较慢，避免用户点击扫码登录时长时间等待
            try:
                import playwright.async_api  # noqa: F401
            except Exception:
                pass

            cfg = core.load_config()
            cfg["port"] = self.port
            cfg["token"] = self.token
            core.save_config(cfg)
            data_dir = core.ensure_data_dirs(cfg)

            log_manager = LogManager()
            formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            file_handler = logging.FileHandler(data_dir / "logs" / "server.log", encoding="utf-8")
            file_handler.setFormatter(formatter)
            file_handler.addFilter(core.UvicornAccessFilter())
            broadcast_handler = core.BroadcastHandler(log_manager.broadcast)
            broadcast_handler.setFormatter(formatter)
            root = logging.getLogger()
            root.setLevel(logging.INFO)
            root.addHandler(file_handler)
            root.addHandler(broadcast_handler)

            runner_inst = runner.Runner(cfg, data_dir)
            # 本地引擎：默认有头模式登录（弹出真实浏览器，可扫码并操作人机验证）
            login_manager = login.LoginManager(data_dir, headless=False)
            scheduler = core.Scheduler(cfg, data_dir, runner_inst)
            app = api.create_app(cfg, data_dir, runner_inst, login_manager, scheduler, log_manager)
            # log_config=None：阻止 uvicorn 接管 logging，避免与我们已配置的 handler 冲突；
            # access_log=False：不输出 "GET /api/..." 每请求访问日志，避免刷屏。
            config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_config=None, access_log=False)
            server = uvicorn.Server(config)
            self._server = server
            server.run()
        except Exception as exc:  # noqa: BLE001
            self._error = str(exc)
