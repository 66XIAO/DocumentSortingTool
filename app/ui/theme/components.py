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

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QVBoxLayout,
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


class ChoiceDialog(QDialog):
    """多选项对话框。需求 13.7 的「恢复执行 / 全部撤销 / 忽略」需要三个出口。

    ``confirm()`` 只有两个按钮，硬塞第三个选项进去只能靠「取消 = 第三种意思」这种
    暗示，而这是一次可能动上千文件的决定，不该靠暗示。选中的下标由 ``chosen`` 给出，
    直接关窗（未选任何按钮）等价于最后一个选项——最后一个按钮约定为最保守的那个。
    """

    def __init__(
        self,
        parent: QWidget,
        title: str,
        body: str,
        options: list[str],
    ) -> None:
        super().__init__(parent)
        if not options:
            raise ValueError("至少要有一个选项")
        self.setWindowTitle(title)
        self.setModal(True)
        self._chosen: int | None = None
        self.buttons: list[QPushButton] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        message = QLabel(body, self)
        message.setWordWrap(True)
        layout.addWidget(message)

        for index, label in enumerate(options):
            button = QPushButton(label, self)
            button.setMinimumHeight(36)
            button.clicked.connect(lambda _=False, i=index: self._pick(i))
            layout.addWidget(button)
            self.buttons.append(button)
        # 最后一个（最保守的）作为默认，回车不会误触发破坏性选项
        self.buttons[-1].setDefault(True)

    @property
    def chosen(self) -> int | None:
        return self._chosen

    def _pick(self, index: int) -> None:
        self._chosen = index
        self.accept()


def choose_option(
    parent: QWidget, title: str, body: str, options: list[str]
) -> int:
    """弹出多选项对话框，返回被选中的下标。直接关窗视为最后一个选项。"""
    dialog = ChoiceDialog(parent, title, body, options)
    dialog.exec()
    return dialog.chosen if dialog.chosen is not None else len(options) - 1


class CountdownDialog(QDialog):
    """确认之后的最后一道闸门：倒计时窗口 + 大号取消按钮。需求 11.4。

    为什么在确认框之后还要再拦一次：确认框是「你是不是想做这件事」，倒计时是
    「你是不是**现在**就想做」。误点击的典型形态是手比脑子快——连点两下正好把确认框
    也点掉。倒计时给出的那 3 秒，是唯一能救回这种误操作的窗口，而且它是**免费**的：
    真心要执行的用户等 3 秒无感，误点的用户少丢一次上千文件。

    取消按钮刻意做大且放在主位：这一刻默认路径应当是「还能反悔」，不是「继续」。
    """

    def __init__(
        self,
        parent: QWidget,
        summary: str,
        seconds: int = 3,
        interval_ms: int = 1000,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("即将开始整理")
        self.setModal(True)
        self._remaining = max(0, seconds)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        self._headline = QLabel(self._headline_text(), self)
        self._headline.setWordWrap(True)
        layout.addWidget(self._headline)

        detail = QLabel(summary, self)
        detail.setWordWrap(True)
        layout.addWidget(detail)

        self._cancel = QPushButton("取消，先不整理", self)
        self._cancel.setMinimumHeight(48)  # 大号：这一刻取消比继续更重要
        self._cancel.setDefault(True)
        self._cancel.clicked.connect(self.reject)
        layout.addWidget(self._cancel)

        self._timer = QTimer(self)
        self._timer.setInterval(max(1, interval_ms))
        self._timer.timeout.connect(self._tick)
        if self._remaining == 0:
            # 供测试与「已确认过的目录」跳过等待；不写成 0 秒特例是为了让调用方
            # 只有一条代码路径
            QTimer.singleShot(0, self.accept)
        else:
            self._timer.start()

    @property
    def remaining(self) -> int:
        return self._remaining

    def _headline_text(self) -> str:
        return f"{self._remaining} 秒后开始移动文件。改主意了就点下面的取消。"

    def _tick(self) -> None:
        self._remaining -= 1
        if self._remaining <= 0:
            self._timer.stop()
            self.accept()
            return
        self._headline.setText(self._headline_text())

    def reject(self) -> None:  # noqa: D102
        self._timer.stop()
        super().reject()


def countdown_to_start(
    parent: QWidget,
    summary: str,
    seconds: int = 3,
    interval_ms: int = 1000,
) -> bool:
    """需求 11.4：确认后给 3 秒取消窗口。返回是否继续执行。"""
    dialog = CountdownDialog(parent, summary, seconds=seconds, interval_ms=interval_ms)
    return dialog.exec() == QDialog.DialogCode.Accepted


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
    "ChoiceDialog",
    "ComboBox",
    "CountdownDialog",
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
    "choose_option",
    "confirm",
    "countdown_to_start",
    "toast_error",
    "toast_info",
]
