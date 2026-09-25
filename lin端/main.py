"""抖音自动续火花 - Linux 自动化服务入口。

用法：
    python main.py                 # 使用 config.json（不存在则自动生成）
    python main.py --port 8766     # 覆盖端口
    python main.py --token xxx     # 覆盖/初始化 Token

首次启动自动创建 config.json、data 目录与 Playwright 数据目录；
程序运行不需要 root 权限。
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import uvicorn

from server import api, core, login, runner
from server.api import LogManager


def setup_logging(data_dir: Path, broadcast) -> None:
    """文件日志 + WebSocket 广播（挂 root logger，核心 reset 不影响）。"""
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(data_dir / "logs" / "server.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(core.UvicornAccessFilter())
    broadcast_handler = core.BroadcastHandler(broadcast)
    broadcast_handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(broadcast_handler)


def main() -> int:
    parser = argparse.ArgumentParser(description="抖音自动续火花服务端")
    parser.add_argument("--port", type=int, default=None, help="覆盖监听端口")
    parser.add_argument("--token", default=None, help="覆盖/初始化 Token")
    args = parser.parse_args()

    try:
        cfg = core.load_config(args.token)
        if args.port:
            cfg["port"] = args.port
        data_dir = core.ensure_data_dirs(cfg)

        log_manager = LogManager()
        setup_logging(data_dir, log_manager.broadcast)

        runner_inst = runner.Runner(cfg, data_dir)
        login_manager = login.LoginManager(data_dir)
        scheduler = core.Scheduler(cfg, data_dir, runner_inst)  # 由 FastAPI lifespan 启动

        app = api.create_app(cfg, data_dir, runner_inst, login_manager, scheduler, log_manager)
        _token_display = core.mask_token(cfg["token"])
        core.LOGGER.info("服务启动: http://%s:%d  Token=%s（完整 Token 见 config.json）", cfg["host"], cfg["port"], _token_display)
        uvicorn.run(app, host=cfg["host"], port=cfg["port"], log_level="warning", access_log=False)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"启动失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
