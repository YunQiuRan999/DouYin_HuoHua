"""主题加载：global.qss 全局样式 + 三模块独立 QSS。

QSS 位于项目根目录 qss/ 下，修改后重启即生效，无需重新打包。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QApplication, QWidget

QSS_DIR = Path(__file__).resolve().parent.parent / "qss"


def _read(name: str) -> str:
    return (QSS_DIR / name).read_text(encoding="utf-8")


def apply_global_theme(app: QApplication) -> None:
    app.setStyleSheet(_read("global.qss"))


def apply_module_theme(widget: QWidget, module: str) -> None:
    """给模块页面应用独立 QSS（status / accounts / schedule）。"""
    widget.setStyleSheet(_read(f"{module}.qss"))
