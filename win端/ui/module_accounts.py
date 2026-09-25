"""账号管理页：账号增删改 + 扫码登录（二维码由服务端生成，本端展示）。"""
from __future__ import annotations

import json
import re
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from api_client import ApiError
from ui.theme import apply_module_theme


class QrLoginDialog(QDialog):
    """扫码登录。

    本地模式：弹出真实浏览器窗口，直接在窗口里扫码/操作人机验证，成功后自动保存；
    远程模式：展示服务端生成的二维码图片，用手机扫码。
    """

    def __init__(self, app, account_id: str, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self.account_id = account_id
        # 本地模式有头（弹真实浏览器窗口，可操作验证码）；远程模式无头（截图二维码）
        self.headless = app.api.config.get("mode", "local") != "local"
        self.setWindowTitle(f"扫码登录 · {account_id}")
        self.setModal(True)
        self.setMinimumSize(340, 460)
        self.storage_state = ""

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        if self.headless:
            title_text = "请使用抖音 App 扫描下方二维码"
            hint_text = "二维码由服务器生成，扫码后请耐心等待自动确认"
        else:
            title_text = "浏览器窗口已打开，请扫码登录"
            hint_text = "如遇人机验证（滑块/图片），请在浏览器窗口中用鼠标直接操作，登录成功后本窗口自动关闭"
        title = QLabel(title_text)
        title.setObjectName("qrTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setWordWrap(True)
        layout.addWidget(title)

        self.image_label = QLabel("正在获取二维码…")
        self.image_label.setObjectName("qrImage")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumHeight(300)
        layout.addWidget(self.image_label, 1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("qrStatus")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        hint = QLabel(hint_text)
        hint.setObjectName("dimLabel")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._poll)
        try:
            self.session_id = self.app.api.start_login(account_id, headless=self.headless)["session_id"]
        except ApiError as exc:
            self.status_label.setText(f"发起登录失败：{exc}")
            return
        self._poll()

    def _poll(self) -> None:
        try:
            status = self.app.api.login_status(self.session_id)
        except ApiError as exc:
            self.status_label.setText(f"查询失败：{exc}")
            self._timer.start()
            return
        state = status["status"]
        if state in ("starting", "pending", "scanning"):
            if self._timer.isActive() is False:
                self._timer.start()
            # 展示服务端实时状态：starting/pending 提示"正在启动浏览器…"，scanning 提示扫码
            self.status_label.setText(status.get("message", "请使用抖音 App 扫码并确认登录…"))
            if state == "scanning":
                self._refresh_image()
        elif state == "success":
            self._timer.stop()
            self.storage_state = status.get("storage_state", "")
            self.status_label.setText("✅ 登录成功，登录态已保存到服务器")
            QTimer.singleShot(800, self.accept)
        else:
            self._timer.stop()
            self.status_label.setText(f"登录失败：{status.get('message', '未知错误')}")

    def _refresh_image(self) -> None:
        try:
            data = self.app.api.login_image(self.session_id)
            pixmap = QPixmap()
            pixmap.loadFromData(data)
            if not pixmap.isNull():
                self.image_label.setPixmap(
                    pixmap.scaled(280, 280, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                )
        except Exception:  # noqa: BLE001  二维码未生成等异常一律静默，轮询会自动重试
            pass

    def closeEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        super().closeEvent(event)


class AccountDialog(QDialog):
    """新增/编辑账号：基本信息 + 凭证（扫码登录/Cookie）+ 调度时间 + 任务。"""

    def __init__(self, app, account: dict | None = None, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        account = account or {}   # 新增模式 account=None → 空 dict
        self.account = account
        self.setWindowTitle("编辑账号" if account else "新增账号")
        self.setModal(True)
        self.setMinimumWidth(520)
        # 限制对话框最大高度为屏幕可用高度的 90%，防止小屏下保存/取消按钮被顶出屏幕
        screen_height = QGuiApplication.primaryScreen().availableGeometry().height() if QGuiApplication.primaryScreen() else 720
        self.setMaximumHeight(int(screen_height * 0.9))

        outer = QVBoxLayout(self)
        outer.setSpacing(8)
        # 内容区放入滚动容器，表单过长时可滚动，按钮始终固定在底部
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setObjectName("dialogScroll")
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSpacing(10)
        layout.setContentsMargins(0, 0, 0, 0)

        form = QFormLayout()
        form.setSpacing(8)
        self.id_edit = QLineEdit(account.get("id", ""))
        self.group_edit = QLineEdit(account.get("group", "默认"))
        self.enabled_check = QCheckBox("启用自动发送")
        self.enabled_check.setChecked(account.get("enabled", True))
        self.headless_check = QCheckBox("无头模式（服务器无显示器时保持开启）")
        self.headless_check.setChecked(account.get("headless", True))
        form.addRow("账号 ID", self.id_edit)
        form.addRow("分组", self.group_edit)
        form.addRow("", self.enabled_check)
        form.addRow("", self.headless_check)
        layout.addLayout(form)

        # 凭证
        credential_title = QLabel("登录凭证")
        credential_title.setObjectName("sectionTitle")
        layout.addWidget(credential_title)
        self.qr_button = QPushButton("📱 扫码登录（在服务器打开抖音二维码）")
        self.qr_button.setObjectName("primaryBtn")
        self.qr_button.clicked.connect(self._qr_login)
        layout.addWidget(self.qr_button)
        self.credential_label = QLabel(self._credential_text())
        self.credential_label.setObjectName("dimLabel")
        layout.addWidget(self.credential_label)
        storage_title = QLabel("或填写已有登录态文件路径（本机/服务器 data 目录下）")
        storage_title.setObjectName("dimLabel")
        layout.addWidget(storage_title)
        self.credential_path_edit = QLineEdit(account.get("credential", ""))
        self.credential_path_edit.setPlaceholderText("storage_state/秋染.json")
        layout.addWidget(self.credential_path_edit)
        cookie_title = QLabel("或粘贴 Cookie JSON（Cookie-Editor 导出）")
        cookie_title.setObjectName("dimLabel")
        layout.addWidget(cookie_title)
        self.cookie_edit = QPlainTextEdit(account.get("cookie_raw", ""))
        self.cookie_edit.setPlaceholderText('[{"name":"sessionid","value":"...","domain":".douyin.com"}, ...]')
        self.cookie_edit.setFixedHeight(70)
        layout.addWidget(self.cookie_edit)

        # 调度时间
        schedule_title = QLabel("每日发送时间（HH:MM，可多个）")
        schedule_title.setObjectName("sectionTitle")
        layout.addWidget(schedule_title)
        self.schedule_list = QListWidget()
        for item in account.get("schedule", []):
            self.schedule_list.addItem(item)
        self.schedule_list.setFixedHeight(66)
        layout.addWidget(self.schedule_list)
        schedule_row = QHBoxLayout()
        add_time = QPushButton("添加时间")
        add_time.setObjectName("miniBtn")
        add_time.clicked.connect(self._add_time)
        del_time = QPushButton("删除选中")
        del_time.setObjectName("miniBtn")
        del_time.clicked.connect(self._del_time)
        schedule_row.addWidget(add_time)
        schedule_row.addWidget(del_time)
        schedule_row.addStretch(1)
        layout.addLayout(schedule_row)

        # 任务
        task_title = QLabel("发送任务")
        task_title.setObjectName("sectionTitle")
        layout.addWidget(task_title)
        task = account.get("task", {})
        friends_title = QLabel("好友（每行一个）")
        friends_title.setObjectName("dimLabel")
        layout.addWidget(friends_title)
        self.friends_edit = QPlainTextEdit("\n".join(task.get("friends", [])))
        self.friends_edit.setFixedHeight(64)
        layout.addWidget(self.friends_edit)
        messages_title = QLabel("消息文案（每行一条；\u2460\uff5e\u2464 可改为随机组，见使用说明）")
        messages_title.setObjectName("dimLabel")
        layout.addWidget(messages_title)
        self.messages_edit = QPlainTextEdit()
        self.messages_edit.setFixedHeight(64)
        self.messages_edit.setPlainText(self._messages_to_lines(task.get("messages", [])))
        layout.addWidget(self.messages_edit)

        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("消息间隔(秒)"))
        self.interval_min = QDoubleSpinBox()
        self.interval_min.setRange(0.5, 600)
        self.interval_min.setValue(task.get("interval_min", 3.0))
        interval_row.addWidget(self.interval_min)
        interval_row.addWidget(QLabel("~"))
        self.interval_max = QDoubleSpinBox()
        self.interval_max.setRange(1, 1200)
        self.interval_max.setValue(task.get("interval_max", 8.0))
        interval_row.addWidget(self.interval_max)
        interval_row.addStretch(1)
        layout.addLayout(interval_row)

        limit_row = QHBoxLayout()
        limit_row.addWidget(QLabel("每日发送上限"))
        self.daily_max_spin = QSpinBox()
        self.daily_max_spin.setRange(0, 100)
        self.daily_max_spin.setValue(int(task.get("daily_max_sends", 20)))
        self.daily_max_spin.setToolTip("单个账号每天最多自动发送给多少个好友；0 表示不限制（不推荐）")
        limit_row.addWidget(self.daily_max_spin)
        limit_row.addWidget(QLabel("好友/天（0=不限制，降低风控风险）"))
        limit_row.addStretch(1)
        layout.addLayout(limit_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        # 内容区放入滚动容器后，再把按钮固定到底部
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        outer.addWidget(buttons)

    # ------------------------------------------------------------- 凭证

    def _credential_text(self) -> str:
        if self.account.get("credential"):
            return f"当前凭证：{self.account['credential']}"
        return "尚未配置凭证（保存前请扫码登录或粘贴 Cookie）"

    def _qr_login(self) -> None:
        account_id = self.id_edit.text().strip()
        if not account_id:
            QMessageBox.information(self, "提示", "请先填写账号 ID")
            return
        dialog = QrLoginDialog(self.app, account_id, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.storage_state:
            self.storage_path = dialog.storage_state
            relative = _relative_storage_path(dialog.storage_state)
            self.credential_label.setText(f"扫码成功！登录态已保存：{relative}")
            self.cookie_edit.clear()

    # ------------------------------------------------------------- 时间

    def _add_time(self) -> None:
        value, ok = QInputDialog.getText(self, "添加发送时间", "时间（HH:MM，24小时制）：")
        if ok and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value.strip()):
            self.schedule_list.addItem(value.strip())
        elif ok:
            QMessageBox.information(self, "提示", "时间格式应为 HH:MM，如 09:30")

    def _del_time(self) -> None:
        for item in self.schedule_list.selectedItems():
            self.schedule_list.takeItem(self.schedule_list.row(item))

    # ------------------------------------------------------------- 消息

    @staticmethod
    def _messages_to_lines(messages: list[dict]) -> str:
        lines = []
        for message in messages:
            if message.get("type") == "text":
                lines.append(str(message.get("content", "")))
            elif message.get("type") == "random":
                choices = message.get("choices", [])
                texts = [c.get("content", "") for c in choices if c.get("type") == "text"]
                if texts:
                    lines.append("|".join(texts))
        return "\n".join(lines)

    def _parse_messages(self) -> list[dict]:
        messages: list[dict] = []
        for line in self.messages_edit.toPlainText().splitlines():
            line = line.strip()
            if not line:
                continue
            if "|" in line:
                choices = [{"type": "text", "content": part.strip()} for part in line.split("|") if part.strip()]
                if choices:
                    messages.append({"type": "random", "choices": choices})
            else:
                messages.append({"type": "text", "content": line})
        return messages

    # ------------------------------------------------------------- 保存

    def _save(self) -> None:
        account_id = self.id_edit.text().strip()
        if not account_id:
            QMessageBox.information(self, "提示", "请填写账号 ID")
            return
        friends = [f.strip() for f in self.friends_edit.toPlainText().splitlines() if f.strip()]
        messages = self._parse_messages()
        if not friends or not messages:
            QMessageBox.information(self, "提示", "任务至少需要一个好友和一条消息文案")
            return
        schedule = [self.schedule_list.item(i).text() for i in range(self.schedule_list.count())]
        credential = self._resolve_credential()
        if credential is None:
            return
        credential_type, credential_path, cookie_raw = credential
        payload = {
            "id": account_id,
            "group": self.group_edit.text().strip() or "默认",
            "enabled": self.enabled_check.isChecked(),
            "credential_type": credential_type,
            "credential": credential_path,
            "cookie_raw": cookie_raw,
            "headless": self.headless_check.isChecked(),
            "schedule": schedule,
            "task": {
                "friends": friends,
                "messages": messages,
                "interval_min": self.interval_min.value(),
                "interval_max": self.interval_max.value(),
                "prevent_duplicates": True,
                "daily_max_sends": self.daily_max_spin.value(),
            },
        }
        try:
            if self.account:
                self.app.api.update_account(self.account["id"], payload)
            else:
                self.app.api.create_account(payload)
        except ApiError as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.accept()

    def _resolve_credential(self):
        """返回 (credential_type, credential, cookie_raw)；Cookie 非法时返回 None。

        优先级：粘贴的 Cookie JSON → 手动填写的登录态文件路径 → 扫码生成的登录态 → 账号已有凭证。
        """
        cookie_text = self.cookie_edit.toPlainText().strip()
        if cookie_text:
            try:
                json.loads(cookie_text)
            except json.JSONDecodeError as exc:
                QMessageBox.warning(self, "提示", f"Cookie 不是有效 JSON：{exc}")
                return None
            return "cookie", "", cookie_text
        manual = self.credential_path_edit.text().strip()
        if manual:
            return "storage", manual, ""
        storage = getattr(self, "storage_path", "") or self.account.get("credential", "")
        if storage:
            return "storage", _relative_storage_path(storage), ""
        return "storage", "", ""


def _relative_storage_path(absolute: str) -> str:
    """把服务端返回的绝对路径转成相对 data_dir 的路径。"""
    marker = "storage_state"
    index = absolute.replace("\\", "/").find(marker)
    if index >= 0:
        return absolute.replace("\\", "/")[index:]
    return absolute


class AccountsPage(QWidget):
    def __init__(self, app) -> None:
        super().__init__()
        self.app = app
        self.setObjectName("accountsPage")
        apply_module_theme(self, "accounts")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        top = QHBoxLayout()
        title = QLabel("账号管理")
        title.setObjectName("sectionTitle")
        top.addWidget(title)
        self.mode_label = QLabel("● 本地引擎")
        self.mode_label.setObjectName("dimLabel")
        top.addWidget(self.mode_label)
        top.addStretch(1)
        add_button = QPushButton("＋ 新增账号")
        add_button.setObjectName("primaryBtn")
        add_button.clicked.connect(self._add)
        top.addWidget(add_button)
        refresh_button = QPushButton("刷新")
        refresh_button.setObjectName("miniBtn")
        refresh_button.clicked.connect(self.refresh)
        top.addWidget(refresh_button)
        layout.addLayout(top)

        self.table = QTableWidget(0, 6)
        self.table.setObjectName("accountsTable")
        self.table.setHorizontalHeaderLabels(["账号", "分组", "启用", "发送时间", "最近状态", "凭证"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setColumnWidth(0, 140)
        self.table.setColumnWidth(1, 80)
        self.table.setColumnWidth(3, 130)
        self.table.doubleClicked.connect(self._edit_selected)
        layout.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        edit_button = QPushButton("编辑选中")
        edit_button.setObjectName("miniBtn")
        edit_button.clicked.connect(self._edit_selected)
        delete_button = QPushButton("删除选中")
        delete_button.setObjectName("dangerBtn")
        delete_button.clicked.connect(self._delete_selected)
        bottom.addWidget(edit_button)
        bottom.addWidget(delete_button)
        bottom.addStretch(1)
        layout.addLayout(bottom)

        self.refresh()

    def append_log(self, text: str) -> None:
        pass  # 日志统一在状态监控页展示

    def on_server_changed(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        mode = self.app.api.config.get("mode", "local")
        if mode == "local":
            target = f"本地引擎 {self.app.api.base_url}"
        else:
            target = f"远程服务器 {self.app.api.base_url}"
        try:
            accounts = self.app.api.list_accounts()
        except ApiError as exc:
            self.table.setRowCount(0)
            self.mode_label.setText(f"● {target}　刷新失败：{exc}")
            return
        self.mode_label.setText(f"● {target}（{len(accounts)} 个账号）")
        self.table.setRowCount(len(accounts))
        for row_index, account in enumerate(accounts):
            last = account.get("last_run") or {}
            self._set(row_index, 0, account.get("id", ""))
            self._set(row_index, 1, account.get("group", "默认"))
            self._set(row_index, 2, "是" if account.get("enabled") else "否")
            self._set(row_index, 3, "、".join(account.get("schedule", [])))
            self._set(row_index, 4, last.get("status", "—"))
            self._set(row_index, 5, account.get("credential", "Cookie") if account.get("credential") else ("Cookie" if account.get("cookie_raw") else "未配置"))

    def _set(self, row: int, col: int, text: str) -> None:
        self.table.setItem(row, col, QTableWidgetItem(text))

    def _selected(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        try:
            accounts = self.app.api.list_accounts()
            return accounts[row] if row < len(accounts) else None
        except ApiError:
            return None

    def _add(self) -> None:
        try:
            dialog = AccountDialog(self.app, None, self)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "错误", f"打开新增账号对话框失败：{exc}")
            return
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()

    def _edit_selected(self) -> None:
        account = self._selected()
        if account is None:
            QMessageBox.information(self, "提示", "请先选中一个账号")
            return
        try:
            dialog = AccountDialog(self.app, account, self)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "错误", f"打开账号编辑对话框失败：{exc}")
            return
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()

    def _delete_selected(self) -> None:
        account = self._selected()
        if account is None:
            QMessageBox.information(self, "提示", "请先选中一个账号")
            return
        reply = QMessageBox.question(
            self, "确认删除",
            f"确定删除账号「{account.get('id')}」吗？\n（服务器上的登录态文件不会删除）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self.app.api.delete_account(account["id"])
        except ApiError as exc:
            QMessageBox.warning(self, "删除失败", str(exc))
            return
        self.refresh()
