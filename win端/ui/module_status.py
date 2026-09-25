"""状态监控页：服务器状态、账号运行概览、实时日志（WebSocket）。"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from api_client import ApiError
from ui.theme import apply_module_theme

COLORS = {"success": "#16a34a", "failed": "#dc2626", "unknown": "#94a3b8", "skipped": "#f59e0b"}
STATUS_LABELS = {"success": "成功", "failed": "失败", "unknown": "未知", "skipped": "已跳过"}
MAX_LOG_LINES = 500  # 日志面板最多保留的行数，防止长期运行导致界面卡顿


class StatusPage(QWidget):
    def __init__(self, app) -> None:
        super().__init__()
        self.app = app
        self.setObjectName("statusPage")
        apply_module_theme(self, "status")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        # 服务器状态卡
        card = QWidget()
        card.setObjectName("card")
        row = QHBoxLayout(card)
        row.setContentsMargins(16, 14, 16, 14)
        self.status_text = QLabel("服务器：未连接")
        self.status_text.setObjectName("statusMain")
        row.addWidget(self.status_text)
        row.addStretch(1)
        self.health_label = QLabel("")
        self.health_label.setObjectName("dimLabel")
        row.addWidget(self.health_label)
        refresh = QPushButton("刷新")
        refresh.setObjectName("miniBtn")
        refresh.clicked.connect(self.refresh)
        row.addWidget(refresh)
        layout.addWidget(card)

        # 账号状态表
        self.table = QTableWidget(0, 8)
        self.table.setObjectName("statusTable")
        self.table.setHorizontalHeaderLabels(
            ["账号", "分组", "启用", "最近状态", "成功", "失败", "发送", "完成时间"]
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setColumnWidth(0, 140)
        self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(3, 80)
        layout.addWidget(self.table, 2)

        # 运行记录表（Win 端隔几天上线时查看服务器每天的运行情况）
        run_title = QLabel("运行记录（最近 30 次）")
        run_title.setObjectName("sectionTitle")
        layout.addWidget(run_title)
        self.runs_table = QTableWidget(0, 6)
        self.runs_table.setObjectName("statusTable")
        self.runs_table.setHorizontalHeaderLabels(
            ["完成时间", "账号", "状态", "成功", "失败", "发送"]
        )
        self.runs_table.horizontalHeader().setStretchLastSection(True)
        self.runs_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.runs_table.verticalHeader().setVisible(False)
        self.runs_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.runs_table.setColumnWidth(0, 160)
        self.runs_table.setColumnWidth(1, 120)
        layout.addWidget(self.runs_table, 2)

        # 日志面板
        log_row = QHBoxLayout()
        log_title = QLabel("实时日志（WebSocket）")
        log_title.setObjectName("sectionTitle")
        log_row.addWidget(log_title)
        log_row.addStretch(1)
        sync_btn = QPushButton("📥 同步服务器日志")
        sync_btn.setObjectName("miniBtn")
        sync_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        sync_btn.clicked.connect(self.sync_server_logs)
        log_row.addWidget(sync_btn)
        layout.addLayout(log_row)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view, 2)

        self._auto_synced = False
        self.refresh()
        # 远程模式下延迟 1.5 秒自动同步一次服务器历史日志（上线即看）
        QTimer.singleShot(1500, self._auto_sync_once)

    def append_log(self, text: str) -> None:
        self.log_view.appendPlainText(text)
        # 超过上限时从顶部裁剪，避免长时运行后界面卡顿
        while self.log_view.blockCount() > MAX_LOG_LINES:
            cursor = self.log_view.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()
        scrollbar = self.log_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def on_server_changed(self) -> None:
        self.refresh()

    def _auto_sync_once(self) -> None:
        """上线后自动同步一次历史日志（仅远程模式，失败静默不打扰）。"""
        if self._auto_synced:
            return
        self._auto_synced = True
        if self.app.api.config.get("mode", "local") == "remote":
            self.sync_server_logs(auto=True)

    def sync_server_logs(self, auto: bool = False) -> None:
        """拉取服务器历史日志并显示在日志面板中（Win端隔几天上线时用）。"""
        from pathlib import Path
        from datetime import datetime

        mode = self.app.api.config.get("mode", "local")
        if mode == "local":
            if not auto:
                self.append_log("=== 本地模式无需同步，日志已实时显示 ===")
            return
        try:
            lines = self.app.api.get_logs(limit=500)
        except ApiError as exc:
            if not auto:
                self.append_log(f"=== 同步服务器日志失败: {exc} ===")
            return
        if not lines:
            if not auto:
                self.append_log("=== 服务器暂无历史日志 ===")
            return
        # 保存到本地文件，方便离线查看
        try:
            log_dir = Path(self.app.api.config_path).parent / "data" / "synced_logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            out_file = log_dir / f"server_logs_{datetime.now():%Y%m%d_%H%M%S}.log"
            out_file.write_text("\n".join(lines), encoding="utf-8")
            self.append_log(f"=== 已同步 {len(lines)} 条服务器日志，已保存到 {out_file.name} ===")
        except Exception:
            self.append_log(f"=== 已同步 {len(lines)} 条服务器日志 ===")
        for line in lines:
            self.append_log(line)
        self.append_log("=== 同步完成，以下为实时日志 ===")

    def refresh(self) -> None:
        mode = self.app.api.config.get("mode", "local")
        try:
            health = self.app.api.health()
        except ApiError as exc:
            if mode == "local":
                self.status_text.setText(f"本地引擎：未就绪（{exc}）")
            else:
                self.status_text.setText(f"服务器：未连接（{exc}）")
            self.health_label.setText("")
            self.table.setRowCount(0)
            return
        if mode == "local":
            self.status_text.setText(
                f"本地引擎：运行中　v{health.get('version', '?')}　"
                f"调度：{'已开启' if health.get('scheduler_enabled') else '已关闭'}　"
                f"任务运行中：{'是' if health.get('running') else '否'}"
            )
        else:
            self.status_text.setText(
                f"服务器：在线　v{health.get('version', '?')}　"
                f"调度：{'已开启' if health.get('scheduler_enabled') else '已关闭'}　"
                f"任务运行中：{'是' if health.get('running') else '否'}"
            )
        self.health_label.setText(f"账号数：{health.get('accounts', 0)}")
        try:
            accounts = self.app.api.list_accounts()
        except ApiError as exc:
            self.health_label.setText(str(exc))
            return
        self.table.setRowCount(len(accounts))
        for row_index, account in enumerate(accounts):
            last = account.get("last_run") or {}
            raw_status = last.get("status", "未知")
            status = STATUS_LABELS.get(raw_status, raw_status)
            self._set(row_index, 0, account.get("id", ""))
            self._set(row_index, 1, account.get("group", "默认"))
            self._set(row_index, 2, "是" if account.get("enabled") else "否")
            self._set(row_index, 3, status, COLORS.get(raw_status, "#94a3b8"))
            self._set(row_index, 4, str(last.get("success", 0)))
            self._set(row_index, 5, str(last.get("failed", 0)))
            self._set(row_index, 6, str(last.get("sent", 0)))
            finished = (last.get("finished_at") or "")[:19].replace("T", " ")
            self._set(row_index, 7, finished)
        self._refresh_runs()

    def _refresh_runs(self) -> None:
        """刷新运行记录表（服务器侧 runs.json，跨天保留）。"""
        try:
            runs = self.app.api.get_runs(limit=30)
        except Exception:  # noqa: BLE001  网络/解析异常一律静默，不阻塞界面
            return
        self.runs_table.setRowCount(len(runs))
        for row_index, run in enumerate(runs):
            raw_status = run.get("status", "未知")
            status = STATUS_LABELS.get(raw_status, raw_status)
            finished = (run.get("finished_at") or run.get("started_at") or "")[:19].replace("T", " ")
            self._set(row_index, 0, finished, target=self.runs_table)
            self._set(row_index, 1, run.get("account_id", ""), target=self.runs_table)
            self._set(row_index, 2, status, COLORS.get(raw_status, "#94a3b8"), target=self.runs_table)
            self._set(row_index, 3, str(run.get("success", 0)), target=self.runs_table)
            self._set(row_index, 4, str(run.get("failed", 0)), target=self.runs_table)
            self._set(row_index, 5, str(run.get("sent", 0)), target=self.runs_table)

    def _set(self, row: int, col: int, text: str, color: str | None = None, target: QTableWidget | None = None) -> None:
        item = QTableWidgetItem(text)
        if color:
            item.setForeground(QColor(color))
        (target or self.table).setItem(row, col, item)
