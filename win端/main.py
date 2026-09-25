"""抖音续火花控制台 - Windows 二合一入口。

一个程序两种运行模式：
- 本地模式（默认）：内置自动化引擎在本机运行，无需 Linux 服务器；
- 远程模式：连接 Linux 服务器（FastAPI 自动化服务）。

数据目录：exe 同级目录（config.json + data/），首次启动自动创建。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _home_dir() -> Path:
    """运行数据目录：打包后为 exe 同级目录，源码运行为项目根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def main() -> int:
    root = _home_dir()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    os.environ["DOUYIN_FIRE_HOME"] = str(root)
    try:
        return _main(root)
    except Exception as exc:  # noqa: BLE001
        import traceback

        try:
            (root / "startup_error.log").write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        return 1


def _main(root: Path) -> int:

    try:
        from embedded_server import EmbeddedServer
    except ImportError:
        EmbeddedServer = None

    embedded = EmbeddedServer(root) if EmbeddedServer is not None else None
    if embedded is not None:
        try:
            embedded.start()  # 启动本地自动化引擎（后台线程）
        except Exception as exc:  # noqa: BLE001
            try:
                (root / "embedded_error.log").write_text(str(exc), encoding="utf-8")
            except Exception:
                pass
            embedded = None
            print(f"警告：本地引擎启动失败，仅可使用远程模式（{exc}）")

    from PySide6.QtWidgets import QApplication, QMessageBox

    from api_client import ApiClient
    from ui import theme
    from ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("抖音续火花控制台")
    theme.apply_global_theme(app)

    api = ApiClient(config_path=root / "client_config.json")
    if embedded is not None and api.config.get("mode", "local") == "local":
        # 本地模式：指向内置引擎
        api.update_server("127.0.0.1", embedded.port, embedded.token)

    if "--smoke-test" in sys.argv:
        # 自检：渲染三页并确认本地引擎可用（供构建后自动化验证）
        import json

        out = os.environ.get("SMOKE_OUT", ".")
        try:
            health = api.health()
            engine_ok = health.get("status") == "ok"
        except Exception as exc:  # noqa: BLE001
            engine_ok = False
            error_text = f"{type(exc).__name__}: {exc}"
            if embedded is not None and getattr(embedded, "_error", ""):
                error_text += f"\n内嵌引擎错误: {embedded._error}"
            os.makedirs(out, exist_ok=True)
            (Path(out) / "engine_error.log").write_text(error_text, encoding="utf-8")
        window = MainWindow(api, embedded=embedded)
        window.show()
        window.ensure_pages_built()
        os.makedirs(out, exist_ok=True)
        for name, page in window._pages.items():
            window.stack.setCurrentWidget(page)
            app.processEvents()
            page.grab().save(os.path.join(out, f"smoke_{name}.png"))

        # UI 对话框自检（防止构造崩溃导致按钮无反应）
        ui_ok = True
        try:
            from ui.module_accounts import AccountDialog
            from ui.settings_dialog import SettingsDialog

            dialog = AccountDialog(window, None, window)
            dialog.close()
            dialog = SettingsDialog(api, embedded, window)
            dialog.close()
        except Exception as exc:  # noqa: BLE001
            ui_ok = False
            (Path(out) / "engine_error.log").write_text(f"UI 自检失败: {exc}", encoding="utf-8")

        window.listener.stop()
        window.listener.wait(3_000)
        window.close()
        return 0 if (engine_ok and ui_ok) else 1

    window = MainWindow(api, embedded=embedded)
    window.show()
    code = app.exec()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
