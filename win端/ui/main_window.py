"""主窗口：左侧三页导航（状态监控 / 账号管理 / 发送时间与文案）+ 服务器设置。

共享 ApiClient 与 WebSocket 日志监听；各模块页面通过对象名挂载样式。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from api_client import ApiClient, LogListener
from ui import module_accounts, module_schedule, module_status
from ui.settings_dialog import SettingsDialog

APP_TITLE = "抖音续火花控制台"

_PAGE_BUILDERS = {
    "status": lambda app: module_status.StatusPage(app),
    "accounts": lambda app: module_accounts.AccountsPage(app),
    "schedule": lambda app: module_schedule.SchedulePage(app),
}


def _log_ui_error(message: str) -> None:
    """把 UI 内部错误写入 exe 同级 ui_error.log，便于排查但绝不阻塞界面。"""
    try:
        from pathlib import Path
        import sys

        root = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
        with (root / "ui_error.log").open("a", encoding="utf-8") as fh:
            fh.write(message + "\n")
    except Exception:
        pass


class MainWindow(QMainWindow):
    def __init__(self, api: ApiClient, embedded=None) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1240, 800)
        self.setMinimumSize(1080, 680)

        self.api = api
        self.embedded = embedded
        self.listener = LogListener(self.api)
        self.listener.line.connect(self._on_log_line)
        self.listener.connected.connect(self._on_ws_connected)

        self._build_ui()
        self._pages = {}
        self._pending_pages = ["status", "accounts", "schedule"]
        if api.config.get("mode", "local") == "local":
            self.conn_label.setText("● 本地引擎")
        QTimer.singleShot(0, self._build_next_page)
        self.listener.start()

    # ------------------------------------------------------------- UI

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_topbar())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_nav())
        self.stack = QStackedWidget()
        self.stack.setObjectName("mainStack")
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)
        self.setCentralWidget(central)

    def _build_topbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("topbar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(20, 10, 20, 10)

        title = QLabel("🔥 抖音续火花控制台")
        title.setObjectName("topbarTitle")
        layout.addWidget(title)
        layout.addStretch(1)

        self.conn_label = QLabel("● 未连接")
        self.conn_label.setObjectName("connBadge")
        layout.addWidget(self.conn_label)

        settings_button = QPushButton("⚙ 服务器设置")
        settings_button.setObjectName("miniBtn")
        settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        settings_button.clicked.connect(self.open_settings)
        layout.addWidget(settings_button)
        return bar

    def _build_nav(self) -> QWidget:
        nav = QWidget()
        nav.setObjectName("nav")
        nav.setFixedWidth(200)
        layout = QVBoxLayout(nav)
        layout.setContentsMargins(14, 20, 14, 16)
        layout.setSpacing(8)

        self.nav_buttons: list[QPushButton] = []
        for key, label in (("status", "状态监控"), ("accounts", "账号管理"), ("schedule", "发送时间与文案")):
            button = QPushButton(label)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.clicked.connect(lambda _=False, k=key: self.switch_page(k))
            self.nav_buttons.append(button)
            layout.addWidget(button)
        self.nav_buttons[0].setChecked(True)

        layout.addStretch(1)
        hint = QLabel("数据与自动化在 Linux 服务端\n本端仅负责监控与配置")
        hint.setObjectName("navHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return nav

    def switch_page(self, key: str) -> None:
        index = {"status": 0, "accounts": 1, "schedule": 2}[key]
        if key not in self._pages:
            self.ensure_pages_built()
        self.stack.setCurrentIndex(index)
        for button, button_key in zip(self.nav_buttons, ("status", "accounts", "schedule")):
            button.setChecked(button_key == key)

    def _build_next_page(self) -> None:
        if not self._pending_pages:
            self.switch_page("status")
            return
        key = self._pending_pages.pop(0)
        try:
            page = _PAGE_BUILDERS[key](self)
        except Exception as exc:  # noqa: BLE001  单页构建失败不阻塞其余页面
            _log_ui_error(f"构建页面 {key} 失败: {exc}")
            QTimer.singleShot(0, self._build_next_page)
            return
        self._pages[key] = page
        self.stack.addWidget(page)
        QTimer.singleShot(0, self._build_next_page)

    def ensure_pages_built(self) -> None:
        while self._pending_pages:
            key = self._pending_pages.pop(0)
            try:
                page = _PAGE_BUILDERS[key](self)
            except Exception as exc:  # noqa: BLE001
                _log_ui_error(f"构建页面 {key} 失败: {exc}")
                continue
            self._pages[key] = page
            self.stack.addWidget(page)
        self.switch_page("status")

    # ------------------------------------------------------------- 行为

    def open_settings(self) -> None:
        try:
            dialog = SettingsDialog(self.api, self.embedded, self)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "错误", f"打开运行设置失败：{exc}")
            return
        if dialog.exec() == SettingsDialog.DialogCode.Accepted:
            # 远程模式：停止本地引擎，释放端口（避免与 SSH 隧道/本地 18765 冲突）；
            # 本地模式：确保本地引擎在运行。
            if self.embedded is not None:
                if self.api.config.get("mode", "local") == "remote":
                    self.embedded.stop()
                else:
                    try:
                        self.embedded.start()
                    except Exception as exc:  # noqa: BLE001
                        QMessageBox.critical(self, "错误", f"本地引擎启动失败：{exc}")
            self.listener.stop()          # 重启日志监听，应用新模式/新地址
            self.listener.wait(3_000)
            self.listener = LogListener(self.api)
            self.listener.line.connect(self._on_log_line)
            self.listener.connected.connect(self._on_ws_connected)
            self.listener.start()
            self._on_ws_connected(False)
            for page in self._pages.values():
                page.on_server_changed()

    def _on_log_line(self, text: str) -> None:
        for page in self._pages.values():
            try:
                page.append_log(text)
            except Exception:  # noqa: BLE001  单页日志分发失败不中断其他页
                pass

    def _on_ws_connected(self, connected: bool) -> None:
        if self.api.config.get("mode", "local") == "local":
            self.conn_label.setText("● 本地引擎")
            return
        self.conn_label.setText("● 已连接服务器" if connected else "● 未连接服务器")
        self.conn_label.setProperty("online", connected)
        self.conn_label.style().unpolish(self.conn_label)
        self.conn_label.style().polish(self.conn_label)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.listener.stop()
        self.listener.wait(3_000)
        super().closeEvent(event)
