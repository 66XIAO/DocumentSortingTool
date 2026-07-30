"""右栏：选中文件的详情。需求 10.7-10.9、8.7。

原路径与目标路径都完整显示且可选中复制——用户要核对某个具体文件去哪了的时候，
省略号中间的路径是没用的。
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFormLayout, QLabel, QVBoxLayout, QWidget

from app.core.models import ConflictKind, PlanItem
from app.ui.theme import (
    CONFLICT,
    SPACE_MD,
    SPACE_SM,
    BodyLabel,
    CaptionLabel,
    StrongBodyLabel,
)

_IMAGE_EXTS = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg"}
)
_THUMBNAIL_MAX = 220


def _selectable(label: QLabel) -> QLabel:
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class DetailPanel(QWidget):
    """文件详情。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_SM)

        self._title = StrongBodyLabel("文件详情", self)
        layout.addWidget(self._title)

        self._empty = CaptionLabel("在中间的结构树里选一个文件", self)
        self._empty.setWordWrap(True)
        layout.addWidget(self._empty)

        self._form_host = QWidget(self)
        form = QFormLayout(self._form_host)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(SPACE_SM)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)

        self._source = _selectable(BodyLabel("", self))
        self._target = _selectable(BodyLabel("", self))
        self._reason = _selectable(BodyLabel("", self))
        self._size = BodyLabel("", self)
        self._mtime = BodyLabel("", self)
        self._status = BodyLabel("", self)

        form.addRow(CaptionLabel("原路径", self), self._source)
        form.addRow(CaptionLabel("目标路径", self), self._target)
        form.addRow(CaptionLabel("命中规则", self), self._reason)
        form.addRow(CaptionLabel("大小", self), self._size)
        form.addRow(CaptionLabel("修改时间", self), self._mtime)
        form.addRow(CaptionLabel("状态", self), self._status)
        layout.addWidget(self._form_host)

        self._preview_label = CaptionLabel("", self)
        layout.addWidget(self._preview_label)

        self._thumbnail = QLabel(self)
        self._thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumbnail.setVisible(False)
        layout.addWidget(self._thumbnail)

        self._text_head = _selectable(CaptionLabel("", self))
        self._text_head.setVisible(False)
        layout.addWidget(self._text_head)

        layout.addStretch(1)
        layout.setContentsMargins(0, 0, 0, SPACE_MD)
        self.clear()

    # -- 对外 -------------------------------------------------------------

    def clear(self) -> None:
        self._empty.setVisible(True)
        self._form_host.setVisible(False)
        self._thumbnail.setVisible(False)
        self._text_head.setVisible(False)
        self._preview_label.setText("")

    def show_item(self, item: PlanItem) -> None:
        self._empty.setVisible(False)
        self._form_host.setVisible(True)

        entry = item.entry
        self._source.setText(str(entry.path))
        self._target.setText(str(item.target))
        self._reason.setText(item.reason or "—")
        self._size.setText(format_size(entry.size))
        self._mtime.setText(_stamp(entry.mtime))
        self._status.setText(self._status_text(item))
        if item.conflict is not ConflictKind.NONE:
            self._status.setStyleSheet(f"color: {CONFLICT};")
        else:
            self._status.setStyleSheet("")

        self._show_preview(item)

    # -- 内部 -------------------------------------------------------------

    @staticmethod
    def _status_text(item: PlanItem) -> str:
        bits = [f"置信度 {item.confidence:.2f}"]
        if item.by_llm:
            bits.append("AI 分类")
        if item.renamed_from:
            bits.append(f"自动改名：{item.renamed_from} → {item.target.name}")
        if item.conflict is not ConflictKind.NONE:
            bits.append(f"冲突：{item.conflict}")
        if not item.included:
            bits.append("未勾选，执行时会跳过")
        if entry_error := item.entry.error:
            bits.append(f"读取问题：{entry_error}")
        return " · ".join(bits)

    def _show_preview(self, item: PlanItem) -> None:
        self._thumbnail.setVisible(False)
        self._text_head.setVisible(False)
        self._preview_label.setText("")

        entry = item.entry
        if entry.ext.lower() in _IMAGE_EXTS:
            pixmap = QPixmap(str(entry.path))
            if not pixmap.isNull():
                self._preview_label.setText("预览")
                self._thumbnail.setPixmap(
                    pixmap.scaled(
                        _THUMBNAIL_MAX,
                        _THUMBNAIL_MAX,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
                self._thumbnail.setVisible(True)
                return

        if entry.text_head:
            self._preview_label.setText("正文开头")
            lines = entry.text_head.splitlines()[:6]
            self._text_head.setText("\n".join(lines))
            self._text_head.setVisible(True)


def _stamp(mtime: float) -> str:
    try:
        return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "—"
