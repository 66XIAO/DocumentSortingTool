"""qfluentwidgets 组件的再导出层。

**这是全应用唯一允许 import qfluentwidgets 的模块。** pages 与 widgets 必须从
``app.ui.theme`` 取组件，由 `tests/unit/test_layering.py` 静态守卫。

理由不是洁癖：qfluentwidgets 在 PyPI 标注 GPLv3（作者另售商业授权，见 tasks.md
任务 58）。若后续因分发方式需要更换组件库，改动面收敛在本模块，pages 与 widgets
一行不用改。

组件库不可用时（未安装、版本不兼容、或将来被替换）自动回落到等价的原生 Qt 控件，
使应用仍可启动——一个能用但样式朴素的窗口，比一个打不开的窗口有用。
"""

from __future__ import annotations

import logging

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QLabel,
    QLineEdit,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QWidget,
)

logger = logging.getLogger(__name__)

FLUENT_AVAILABLE = True

try:
    from qfluentwidgets import (
        BodyLabel,
        CaptionLabel,
        CheckBox,
        ComboBox,
        FluentWindow,
        IndeterminateProgressBar,
        InfoBar,
        InfoBarPosition,
        LineEdit,
        MessageBox,
        NavigationItemPosition,
        PrimaryPushButton,
        ProgressBar,
        PushButton,
        StrongBodyLabel,
        SubtitleLabel,
        SwitchButton,
        Theme,
        TitleLabel,
        TreeWidget,
        setTheme,
    )
    from qfluentwidgets import FluentIcon as FIF
except ImportError as exc:  # pragma: no cover - 依赖已锁定，仅作降级兜底
    FLUENT_AVAILABLE = False
    logger.warning("qfluentwidgets 不可用，回落到原生 Qt 控件: %s", exc)

    BodyLabel = QLabel  # type: ignore[assignment,misc]
    CaptionLabel = QLabel  # type: ignore[assignment,misc]
    StrongBodyLabel = QLabel  # type: ignore[assignment,misc]
    SubtitleLabel = QLabel  # type: ignore[assignment,misc]
    TitleLabel = QLabel  # type: ignore[assignment,misc]
    CheckBox = QCheckBox  # type: ignore[assignment,misc]
    ComboBox = QComboBox  # type: ignore[assignment,misc]
    LineEdit = QLineEdit  # type: ignore[assignment,misc]
    PushButton = QPushButton  # type: ignore[assignment,misc]
    PrimaryPushButton = QPushButton  # type: ignore[assignment,misc]
    ProgressBar = QProgressBar  # type: ignore[assignment,misc]
    IndeterminateProgressBar = QProgressBar  # type: ignore[assignment,misc]
    TreeWidget = QTreeWidget  # type: ignore[assignment,misc]
    FluentWindow = QMainWindow  # type: ignore[assignment,misc]
    SwitchButton = QCheckBox  # type: ignore[assignment,misc]
    MessageBox = None  # type: ignore[assignment]
    InfoBar = None  # type: ignore[assignment]
    InfoBarPosition = None  # type: ignore[assignment]
    NavigationItemPosition = None  # type: ignore[assignment]
    Theme = None  # type: ignore[assignment]
    FIF = None  # type: ignore[assignment]

    def setTheme(*_args: object, **_kw: object) -> None:  # noqa: N802
        pass


def apply_system_theme() -> None:
    """主题跟随系统深浅色。需求 17.7。"""
    if FLUENT_AVAILABLE and Theme is not None:
        setTheme(Theme.AUTO)


def confirm(parent: QWidget, title: str, body: str, ok_text: str, cancel_text: str = "取消") -> bool:
    """确认对话框。返回用户是否确认。

    这是全应用唯一的确认入口——需求 11.3、20.3、20.4、19.10 都要求「二次确认」，
    集中一处能保证文案与行为一致。
    """
    if FLUENT_AVAILABLE and MessageBox is not None:
        box = MessageBox(title, body, parent)
        box.yesButton.setText(ok_text)
        box.cancelButton.setText(cancel_text)
        return bool(box.exec())

    from PySide6.QtWidgets import QMessageBox

    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(body)
    box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
    box.button(QMessageBox.StandardButton.Ok).setText(ok_text)
    box.button(QMessageBox.StandardButton.Cancel).setText(cancel_text)
    return box.exec() == QMessageBox.StandardButton.Ok


def toast_error(parent: QWidget, title: str, body: str) -> None:
    """非阻塞的错误提示。"""
    if FLUENT_AVAILABLE and InfoBar is not None and InfoBarPosition is not None:
        InfoBar.error(
            title=title,
            content=body,
            parent=parent,
            position=InfoBarPosition.TOP,
            duration=5000,
        )
    else:  # pragma: no cover - 降级路径
        logger.error("%s: %s", title, body)


def toast_info(parent: QWidget, title: str, body: str) -> None:
    if FLUENT_AVAILABLE and InfoBar is not None and InfoBarPosition is not None:
        InfoBar.success(
            title=title,
            content=body,
            parent=parent,
            position=InfoBarPosition.TOP,
            duration=3000,
        )
    else:  # pragma: no cover
        logger.info("%s: %s", title, body)


__all__ = [
    "BodyLabel",
    "CaptionLabel",
    "CheckBox",
    "ComboBox",
    "FIF",
    "FLUENT_AVAILABLE",
    "FluentWindow",
    "IndeterminateProgressBar",
    "LineEdit",
    "NavigationItemPosition",
    "PrimaryPushButton",
    "ProgressBar",
    "PushButton",
    "StrongBodyLabel",
    "SubtitleLabel",
    "SwitchButton",
    "TitleLabel",
    "TreeWidget",
    "apply_system_theme",
    "confirm",
    "toast_error",
    "toast_info",
]
