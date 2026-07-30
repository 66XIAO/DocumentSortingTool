"""空态、骨架屏、错误态。需求 17.9-17.11。

三者都只接受「摘要 + 主动作」两个参数，页面只提供文案。这样「无数据时该说什么」
的决定权留在页面，而「怎么摆」的决定权留在这里。
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QVBoxLayout, QWidget

from app.ui.theme import (
    DANGER,
    SPACE_MD,
    SPACE_SM,
    SPACE_XL,
    BodyLabel,
    CaptionLabel,
    IndeterminateProgressBar,
    PrimaryPushButton,
    StrongBodyLabel,
)


class _CenteredPanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(SPACE_XL, SPACE_XL, SPACE_XL, SPACE_XL)
        self._layout.setSpacing(SPACE_MD)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def add(self, widget: QWidget) -> None:
        self._layout.addWidget(widget, alignment=Qt.AlignmentFlag.AlignHCenter)


class EmptyState(_CenteredPanel):
    """空态：说明现状 + 给出下一步动作。需求 17.9。"""

    def __init__(
        self,
        title: str,
        hint: str = "",
        action_text: str = "",
        on_action: Callable[[], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._title = StrongBodyLabel(title, self)
        self.add(self._title)

        self._hint = CaptionLabel(hint, self)
        self._hint.setWordWrap(True)
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint.setVisible(bool(hint))
        self.add(self._hint)

        self._button = PrimaryPushButton(action_text or "", self)
        self._button.setVisible(bool(action_text))
        if on_action is not None:
            self._button.clicked.connect(lambda: on_action())
        self.add(self._button)

    def set_text(self, title: str, hint: str = "") -> None:
        self._title.setText(title)
        self._hint.setText(hint)
        self._hint.setVisible(bool(hint))


class SkeletonState(_CenteredPanel):
    """骨架屏：扫描进行中的占位。需求 17.10。

    用不确定进度条而非假的灰条：扫描初期总量未知，假灰条会暗示「马上就好」，
    不确定进度条如实表达「正在数，还不知道有多少」。
    """

    def __init__(self, message: str = "正在扫描…", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._label = StrongBodyLabel(message, self)
        self.add(self._label)

        self._bar = IndeterminateProgressBar(self)
        self._bar.setFixedWidth(240)
        self.add(self._bar)

        self._detail = CaptionLabel("", self)
        self._detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.add(self._detail)

    def set_message(self, message: str) -> None:
        self._label.setText(message)

    def set_detail(self, detail: str) -> None:
        self._detail.setText(detail)


class ErrorState(_CenteredPanel):
    """错误态：摘要 + 重试。需求 17.11。"""

    def __init__(
        self,
        title: str = "出错了",
        detail: str = "",
        action_text: str = "重试",
        on_action: Callable[[], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._title = StrongBodyLabel(title, self)
        self._title.setStyleSheet(f"color: {DANGER};")
        self.add(self._title)

        self._detail = BodyLabel(detail, self)
        self._detail.setWordWrap(True)
        self._detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.add(self._detail)

        self._button = PrimaryPushButton(action_text, self)
        self._button.setVisible(on_action is not None)
        if on_action is not None:
            self._button.clicked.connect(lambda: on_action())
        self.add(self._button)

        self._layout.setSpacing(SPACE_SM)

    def set_error(self, title: str, detail: str) -> None:
        self._title.setText(title)
        self._detail.setText(detail)
