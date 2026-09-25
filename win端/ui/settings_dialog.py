"""连接设置对话框：运行模式（本地内置引擎 / 连接远程 Linux 服务器）+ 服务器信息。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from api_client import ApiClient, ApiError


class SettingsDialog(QDialog):
    """选择运行模式并编辑连接信息（持久化到客户端本地 client_config.json）。"""

    def __init__(self, api: ApiClient, embedded, parent=None) -> None:
        super().__init__(parent)
        self.api = api
        self.embedded = embedded
        self.setWindowTitle("运行设置")
        self.setModal(True)
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        title = QLabel("运行模式")
        title.setObjectName("settingsTitle")
        layout.addWidget(title)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("本地运行（本机直接发送，无需服务器）", "local")
        self.mode_combo.addItem("连接远程服务器（Linux 自动化服务）", "remote")
        if api.config.get("mode", "local") == "remote":
            self.mode_combo.setCurrentIndex(1)
        self.mode_combo.currentIndexChanged.connect(self._sync_visibility)
        layout.addWidget(self.mode_combo)

        # 本地模式信息
        self.local_info = QLabel()
        self.local_info.setObjectName("dimLabel")
        self.local_info.setWordWrap(True)
        layout.addWidget(self.local_info)

        # 远程模式表单（QFormLayout 无 setVisible，包一层 QWidget）
        server = api.config["server"]
        self.form_widget = QWidget()
        form = QFormLayout(self.form_widget)
        form.setSpacing(8)
        self.host_edit = QLineEdit(server["host"])
        self.host_edit.setPlaceholderText("如 192.168.1.100")
        self.port_edit = QSpinBox()
        self.port_edit.setRange(1, 65535)
        self.port_edit.setValue(server["port"])
        self.token_edit = QLineEdit(server["token"])
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText("服务端 config.json 中的 Token")
        form.addRow("服务器 IP", self.host_edit)
        form.addRow("端口", self.port_edit)
        form.addRow("Token", self.token_edit)
        layout.addWidget(self.form_widget)

        hint = QLabel("远程模式时，Token 查看方式：cat config.json | grep token")
        hint.setObjectName("dimLabel")
        layout.addWidget(hint)

        test_button = QPushButton("测试连接")
        test_button.setObjectName("miniBtn")
        test_button.clicked.connect(self._test_remote)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        row = QHBoxLayout()
        row.addWidget(test_button)
        row.addStretch(1)
        row.addWidget(buttons)
        layout.addLayout(row)

        self._sync_visibility()

    def _sync_visibility(self) -> None:
        is_local = self.mode_combo.currentData() == "local"
        self.local_info.setVisible(is_local)
        self.form_widget.setVisible(not is_local)
        if is_local and self.embedded is not None:
            self.local_info.setText(
                f"内置自动化引擎运行在本机 127.0.0.1:{self.embedded.port}，"
                "数据（账号/登录态/日志）保存在程序同级目录 config.json 与 data/ 中。"
            )

    def _test_remote(self) -> None:
        temp = ApiClient()
        temp.config["server"]["host"] = self.host_edit.text().strip()
        temp.config["server"]["port"] = self.port_edit.value()
        temp.config["server"]["token"] = self.token_edit.text().strip()
        try:
            health = temp.health()
        except ApiError as exc:
            QMessageBox.warning(self, "连接失败", f"无法连接服务器：{exc}")
            return
        # /api/health 免鉴权，仅代表"服务在线"；必须再带 Token 请求受保护接口，
        # 否则 Token 填错也会误报"连接成功"，导致其他页面报"Token 无效或未授权"。
        try:
            temp.request("GET", "/api/schedule")
        except ApiError as exc:
            QMessageBox.warning(
                self, "Token 无效或未授权",
                f"服务器在线，但 Token 校验失败：{exc}\n\n"
                "请确认 Token 正确：在服务器执行 cat config.json，复制 token 字段的值（不含引号）。",
            )
            return
        QMessageBox.information(
            self, "连接成功",
            f"服务端在线 v{health.get('version', '?')}，Token 校验通过\n"
            f"账号数: {health.get('accounts', 0)}",
        )

    def _save(self) -> None:
        if self.mode_combo.currentData() == "local":
            if self.embedded is not None:
                self.api.set_mode("local")
                self.api.update_server("127.0.0.1", self.embedded.port, self.embedded.token)
            self.accept()
            return
        host = self.host_edit.text().strip()
        if not host:
            QMessageBox.information(self, "提示", "请填写服务器 IP")
            return
        port = self.port_edit.value()
        # 隧道端口冲突警告：127.0.0.1 且端口落在本地引擎占用段（18765-18804）时，
        # 请求会打到本地引擎而不是服务器，导致"账号还是本地的、刷新没反应"。
        if host in ("127.0.0.1", "localhost") and 18765 <= port <= 18804:
            reply = QMessageBox.warning(
                self, "端口冲突",
                f"端口 {port} 是本地引擎的默认端口段（18765-18804）。\n\n"
                "如果这是 SSH 隧道转发端口，请改用一个不冲突的端口（例如 28888），"
                "并把隧道脚本里 LOCAL_PORT 改成同一个数字。",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Ok:
                return
        temp = ApiClient()
        temp.config["server"]["host"] = host
        temp.config["server"]["port"] = port
        temp.config["server"]["token"] = self.token_edit.text().strip()
        try:
            temp.health()
        except ApiError as exc:
            reply = QMessageBox.question(
                self, "无法连接",
                f"当前无法连接服务器：{exc}\n仍然保存设置吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        else:
            # 服务器在线但 Token 错误时，直接保存会把错误 Token 固化，
            # 导致其他页面一直报"Token 无效或未授权"。此时应阻止保存。
            try:
                temp.request("GET", "/api/schedule")
            except ApiError as exc:
                QMessageBox.warning(
                    self, "Token 无效或未授权",
                    f"服务器在线，但 Token 校验失败：{exc}\n\n"
                    "请确认 Token 正确：在服务器执行 cat config.json，复制 token 字段的值（不含引号）。",
                )
                return
        self.api.set_mode("remote")
        self.api.update_server(host, port, self.token_edit.text())
        self.accept()
