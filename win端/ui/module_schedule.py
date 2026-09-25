"""发送时间与文案页：按账号配置好友/消息/发送时间，手动触发运行。

任务数据保存到服务端（PUT /api/accounts）；运行结果实时显示在状态监控页日志。
"""
from __future__ import annotations

import re

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from api_client import ApiError
from ui.theme import apply_module_theme

STICKERS = ["比心", "开心", "抱抱", "亲亲", "加油", "晚安"]


class RunWorker(QThread):
    """后台执行运行请求（HTTP 等待期间不阻塞 UI 线程）。"""

    done = Signal(dict)     # 服务端返回的 {record: {...}}
    error = Signal(str)     # ApiError 或其他异常信息

    def __init__(self, api, account_id: str, dry_run: bool, parent=None) -> None:
        super().__init__(parent)
        self.api = api
        self.account_id = account_id
        self.dry_run = dry_run

    def run(self) -> None:
        try:
            # 手动运行/试运行始终强制发送，绕过当日防重复，保证结果真实可测
            response = self.api.run_account(self.account_id, dry_run=self.dry_run, force=True, timeout=1800)
            self.done.emit(response or {})
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))


class MessageDialog(QDialog):
    """编辑一条消息：普通文本 / 随机组 / 抖音表情。"""

    def __init__(self, parent=None, message: dict | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑消息")
        self.setModal(True)
        self.setMinimumWidth(420)
        message = message or {}

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self.type_combo = QComboBox()
        self.type_combo.addItem("普通文本", "text")
        self.type_combo.addItem("随机组（每次随机选一条）", "random")
        self.type_combo.addItem("抖音原生表情", "douyin_sticker")
        if message.get("type") == "random":
            self.type_combo.setCurrentIndex(1)
        elif message.get("type") == "douyin_sticker":
            self.type_combo.setCurrentIndex(2)
        layout.addWidget(self.type_combo)

        self.text_edit = QLineEdit(str(message.get("content", "")) if message.get("type") == "text" else "")
        self.text_edit.setPlaceholderText("输入消息内容")
        layout.addWidget(self.text_edit)

        self.random_edit = QPlainTextEdit()
        self.random_edit.setPlaceholderText("每行一个选项，发送时随机选一条")
        if message.get("type") == "random":
            choices = [c.get("content", "") for c in message.get("choices", []) if c.get("type") == "text"]
            self.random_edit.setPlainText("\n".join(choices))
        self.random_edit.setVisible(message.get("type") == "random")
        layout.addWidget(self.random_edit)

        self.sticker_combo = QComboBox()
        self.sticker_combo.addItems(STICKERS)
        if message.get("type") == "douyin_sticker":
            index = STICKERS.index(str(message.get("sticker"))) if message.get("sticker") in STICKERS else 0
            self.sticker_combo.setCurrentIndex(index)
        self.sticker_combo.setVisible(message.get("type") == "douyin_sticker")
        layout.addWidget(self.sticker_combo)

        self.type_combo.currentIndexChanged.connect(self._sync_visibility)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _sync_visibility(self) -> None:
        message_type = self.type_combo.currentData()
        self.text_edit.setVisible(message_type == "text")
        self.random_edit.setVisible(message_type == "random")
        self.sticker_combo.setVisible(message_type == "douyin_sticker")

    def message(self) -> dict:
        message_type = self.type_combo.currentData()
        if message_type == "random":
            choices = [{"type": "text", "content": line.strip()} for line in self.random_edit.toPlainText().splitlines() if line.strip()]
            return {"type": "random", "choices": choices}
        if message_type == "douyin_sticker":
            return {"type": "douyin_sticker", "sticker": self.sticker_combo.currentText()}
        return {"type": "text", "content": self.text_edit.text().strip()}


class SchedulePage(QWidget):
    def __init__(self, app) -> None:
        super().__init__()
        self.app = app
        self.setObjectName("schedulePage")
        apply_module_theme(self, "schedule")
        self._accounts: list[dict] = []
        self._current: dict | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        top = QHBoxLayout()
        top.addWidget(QLabel("账号"))
        self.account_combo = QComboBox()
        self.account_combo.currentIndexChanged.connect(self._load_current)
        top.addWidget(self.account_combo, 1)
        refresh = QPushButton("刷新")
        refresh.setObjectName("miniBtn")
        refresh.clicked.connect(self.refresh)
        top.addWidget(refresh)
        layout.addLayout(top)

        body = QHBoxLayout()
        body.setSpacing(12)

        # 左：好友
        left = QVBoxLayout()
        left.addWidget(self._label("好友列表"))
        self.friends_list = QListWidget()
        left.addWidget(self.friends_list, 1)
        friend_row = QHBoxLayout()
        add_friend = QPushButton("＋ 好友")
        add_friend.setObjectName("miniBtn")
        add_friend.clicked.connect(self._add_friend)
        del_friend = QPushButton("－ 选中")
        del_friend.setObjectName("miniBtn")
        del_friend.clicked.connect(lambda: self._del_selected_item(self.friends_list))
        friend_row.addWidget(add_friend)
        friend_row.addWidget(del_friend)
        friend_row.addStretch(1)
        left.addLayout(friend_row)
        body.addLayout(left, 1)

        # 中：消息
        middle = QVBoxLayout()
        middle.addWidget(self._label("消息文案"))
        self.messages_list = QListWidget()
        middle.addWidget(self.messages_list, 1)
        message_row = QHBoxLayout()
        add_message = QPushButton("＋ 消息")
        add_message.setObjectName("miniBtn")
        add_message.clicked.connect(self._add_message)
        edit_message = QPushButton("编辑选中")
        edit_message.setObjectName("miniBtn")
        edit_message.clicked.connect(self._edit_message)
        del_message = QPushButton("－ 选中")
        del_message.setObjectName("miniBtn")
        del_message.clicked.connect(self._del_message)
        message_row.addWidget(add_message)
        message_row.addWidget(edit_message)
        message_row.addWidget(del_message)
        message_row.addStretch(1)
        middle.addLayout(message_row)
        body.addLayout(middle, 2)

        # 右：时间 + 控制
        right = QVBoxLayout()
        right.addWidget(self._label("每日发送时间"))
        self.times_list = QListWidget()
        right.addWidget(self.times_list)
        time_row = QHBoxLayout()
        add_time = QPushButton("＋ 时间")
        add_time.setObjectName("miniBtn")
        add_time.clicked.connect(self._add_time)
        del_time = QPushButton("－ 选中")
        del_time.setObjectName("miniBtn")
        del_time.clicked.connect(lambda: self._del_selected_item(self.times_list))
        time_row.addWidget(add_time)
        time_row.addWidget(del_time)
        time_row.addStretch(1)
        right.addLayout(time_row)

        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("间隔(秒)"))
        self.interval_min = QDoubleSpinBox()
        self.interval_min.setRange(0.5, 600)
        self.interval_min.setValue(3.0)
        interval_row.addWidget(self.interval_min)
        interval_row.addWidget(QLabel("~"))
        self.interval_max = QDoubleSpinBox()
        self.interval_max.setRange(1, 1200)
        self.interval_max.setValue(8.0)
        interval_row.addWidget(self.interval_max)
        right.addLayout(interval_row)

        self.scheduler_check = QPushButton("自动调度：已开启")
        self.scheduler_check.setObjectName("primaryBtn")
        self.scheduler_check.clicked.connect(self._toggle_schedule)
        right.addWidget(self.scheduler_check)

        save_button = QPushButton("保存配置到服务器")
        save_button.setObjectName("primaryBtn")
        save_button.clicked.connect(self._save)
        right.addWidget(save_button)

        run_row = QHBoxLayout()
        self.run_button = QPushButton("▶ 立即运行")
        self.run_button.setObjectName("primaryBtn")
        self.run_button.clicked.connect(lambda: self._run(dry_run=False))
        self.dry_button = QPushButton("试运行")
        self.dry_button.setObjectName("miniBtn")
        self.dry_button.clicked.connect(lambda: self._run(dry_run=True))
        run_row.addWidget(self.run_button)
        run_row.addWidget(self.dry_button)
        run_row.addStretch(1)
        right.addLayout(run_row)

        self.result_label = QLabel("")
        self.result_label.setObjectName("dimLabel")
        self.result_label.setWordWrap(True)
        right.addWidget(self.result_label)
        right.addStretch(1)
        body.addLayout(right, 1)

        layout.addLayout(body, 1)

        self.refresh()

    # ------------------------------------------------------------- 基础

    @staticmethod
    def _label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("sectionTitle")
        return label

    def append_log(self, text: str) -> None:
        pass

    def on_server_changed(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        try:
            self._accounts = self.app.api.list_accounts()
        except ApiError:
            self._accounts = []
        current_id = self.account_combo.currentData()
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        for account in self._accounts:
            self.account_combo.addItem(f"{account.get('id')}（{account.get('group', '默认')}）", account.get("id"))
        self.account_combo.blockSignals(False)
        if current_id:
            index = self.account_combo.findData(current_id)
            if index >= 0:
                self.account_combo.setCurrentIndex(index)
        self._load_current()
        try:
            schedule = self.app.api.get_schedule()
            enabled = schedule.get("enabled", True)
        except ApiError:
            enabled = True
        self.scheduler_check.setText(f"自动调度：{'已开启' if enabled else '已关闭'}")

    def _load_current(self) -> None:
        account_id = self.account_combo.currentData()
        self._current = next((a for a in self._accounts if a.get("id") == account_id), None)
        task = (self._current or {}).get("task", {})
        self.friends_list.clear()
        self.friends_list.addItems(task.get("friends", []))
        self.messages_list.clear()
        for message in task.get("messages", []):
            self._append_message(message)
        self.times_list.clear()
        self.times_list.addItems((self._current or {}).get("schedule", []))
        self.interval_min.setValue(task.get("interval_min", 3.0))
        self.interval_max.setValue(task.get("interval_max", 8.0))

    # ------------------------------------------------------------- 编辑

    def _add_friend(self) -> None:
        name, ok = QInputDialog.getText(self, "添加好友", "好友昵称：")
        if ok and name.strip():
            self.friends_list.addItem(name.strip())

    def _del_selected_item(self, target_list) -> None:
        for item in target_list.selectedItems():
            target_list.takeItem(target_list.row(item))

    def _add_message(self) -> None:
        dialog = MessageDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._append_message(dialog.message())

    def _append_message(self, message: dict) -> None:
        item = QListWidgetItem(_message_summary(message))
        item.setData(Qt.ItemDataRole.UserRole, message)
        self.messages_list.addItem(item)

    def _edit_message(self) -> None:
        row = self.messages_list.currentRow()
        item = self.messages_list.item(row) if row >= 0 else None
        if item is None:
            QMessageBox.information(self, "提示", "请先选中一条消息")
            return
        dialog = MessageDialog(self, item.data(Qt.ItemDataRole.UserRole))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            updated = dialog.message()
            item.setData(Qt.ItemDataRole.UserRole, updated)
            item.setText(_message_summary(updated))

    def _del_message(self) -> None:
        self._del_selected_item(self.messages_list)

    def _add_time(self) -> None:
        value, ok = QInputDialog.getText(self, "添加发送时间", "时间（HH:MM，24小时制）：")
        if ok and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value.strip()):
            self.times_list.addItem(value.strip())
        elif ok:
            QMessageBox.information(self, "提示", "时间格式应为 HH:MM，如 09:30")

    def _del_time(self) -> None:
        self._del_selected_item(self.times_list)

    # ------------------------------------------------------------- 保存/运行

    def _collect_task(self) -> dict | None:
        if self._current is None:
            QMessageBox.information(self, "提示", "请先选择账号")
            return None
        friends = [self.friends_list.item(i).text() for i in range(self.friends_list.count())]
        if not friends:
            QMessageBox.information(self, "提示", "好友列表不能为空")
            return None
        messages = [
            self.messages_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.messages_list.count())
        ]
        if not messages:
            QMessageBox.information(self, "提示", "消息文案不能为空")
            return None
        return {
            "friends": friends,
            "messages": messages,
            "interval_min": self.interval_min.value(),
            "interval_max": self.interval_max.value(),
            "prevent_duplicates": True,
        }

    def _save(self) -> None:
        if self._current is None:
            return
        task = self._collect_task()
        if task is None:
            return
        payload = dict(self._current)
        payload["task"] = task
        payload["schedule"] = [self.times_list.item(i).text() for i in range(self.times_list.count())]
        try:
            self.app.api.update_account(self._current["id"], payload)
        except ApiError as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.result_label.setText("已保存到服务器")
        self.refresh()

    def _run(self, dry_run: bool) -> None:
        if self._current is None:
            QMessageBox.information(self, "提示", "请先选择账号")
            return
        account_id = self._current["id"]
        self.run_button.setEnabled(False)
        self.dry_button.setEnabled(False)
        self.result_label.setText(f"{'试运行' if dry_run else '运行'}中，完成后将弹出结果提示…")
        self._worker = RunWorker(self.app.api, account_id, dry_run, self)
        self._worker.done.connect(lambda response: self._on_run_done(dry_run, response))
        self._worker.error.connect(self._on_run_error)
        self._worker.start()

    def _on_run_done(self, dry_run: bool, response: dict) -> None:
        record = response.get("record", {}) if isinstance(response, dict) else {}
        status = record.get("status", "?")
        success = int(record.get("success", 0) or 0)
        failed = int(record.get("failed", 0) or 0)
        sent = int(record.get("sent", 0) or 0)
        failures = record.get("failures", []) or []
        lines = [f"执行完成：{status}（成功 {success}，失败 {failed}，发送 {sent} 条）"]
        for item in failures:
            lines.append(f"　· {item.get('friend', '')}: {item.get('error', '')}")
        self.result_label.setText("\n".join(lines))
        self._show_result_dialog(dry_run, status, success, failed, sent, failures)
        self.run_button.setEnabled(True)
        self.dry_button.setEnabled(True)
        self.refresh()

    def _on_run_error(self, message: str) -> None:
        self.result_label.setText(f"运行失败：{message}")
        box = QMessageBox(self)
        box.setWindowTitle("❌ 错误发送")
        box.setIcon(QMessageBox.Icon.Critical)
        box.setText("任务启动失败，消息未发送")
        box.setDetailedText(message)
        box.exec()
        self.run_button.setEnabled(True)
        self.dry_button.setEnabled(True)
        self.refresh()

    def _show_result_dialog(self, dry_run: bool, status: str, success: int, failed: int, sent: int, failures: list) -> None:
        if dry_run:
            if status == "success":
                QMessageBox.information(
                    self, "试运行通过",
                    "登录状态正常，全部好友均可正常访问。\n\n试运行不会真正发送消息，如需发送请点击「立即运行」。",
                )
            else:
                self._show_failure_dialog("试运行失败", failures or [{"friend": "", "error": f"未通过（状态 {status}）"}])
            return
        if status == "skipped":
            QMessageBox.information(
                self, "已跳过",
                "当天已发送过全部消息（自动防重复）。\n\n如需重发，请点击「立即运行」手动强制发送。",
            )
        elif status == "failed" or failed > 0 or failures:
            self._show_failure_dialog("❌ 错误发送", failures)
        elif status == "success":
            QMessageBox.information(
                self, "✅ 成功发送",
                f"成功 {success} 个好友，共发送 {sent} 条消息，失败 {failed} 个。",
            )
        else:
            self._show_failure_dialog("结果未知", failures or [{"friend": "", "error": f"未知状态：{status}"}])

    def _show_failure_dialog(self, title: str, failures: list) -> None:
        failed_lines = [f"　· {item.get('friend', '') or '(未知)'}: {item.get('error', '') or '未知错误'}" for item in failures]
        if not failed_lines:
            failed_lines = ["　· (未知): 未返回具体失败信息，请查看状态监控页的实时日志"]
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("部分或全部消息发送失败，失败明细如下：")
        box.setDetailedText("\n".join(failed_lines))
        box.exec()

    def _toggle_schedule(self) -> None:
        enabled = self.scheduler_check.text().endswith("已开启")
        try:
            result = self.app.api.set_schedule(not enabled)
        except ApiError as exc:
            QMessageBox.warning(self, "操作失败", str(exc))
            return
        self.scheduler_check.setText(f"自动调度：{'已开启' if result['enabled'] else '已关闭'}")

    def closeEvent(self, event) -> None:  # noqa: N802
        """窗口关闭时等待正在运行的后台任务安全退出，避免线程悬挂。"""
        worker = getattr(self, "_worker", None)
        if worker is not None and worker.isRunning():
            worker.wait(3_000)
        super().closeEvent(event)


def _message_summary(message: dict) -> str:
    if message.get("type") == "random":
        choices = [c.get("content", "") for c in message.get("choices", []) if c.get("type") == "text"]
        return f"[随机]：{' | '.join(choices)}"
    if message.get("type") == "douyin_sticker":
        return f"[表情]：{message.get('sticker', '')}"
    return f"[文本]：{message.get('content', '')}"
