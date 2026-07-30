"""选择目录页。需求 1.8、17.2。

准入被拒时不只说「不行」，而是按原因码给出具体原因与可行的下一步——用户选错目录
是常态，一句「该目录不可用」只会让人反复试。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from app.core.models import ScanOptions
from app.core.safety import AdmissionResult
from app.ui.theme import (
    DANGER,
    SPACE_LG,
    SPACE_MD,
    SPACE_SM,
    SPACE_XL,
    SPACE_XS,
    BodyLabel,
    CaptionLabel,
    CheckBox,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
    TitleLabel,
)

_MAX_RECENT = 6


class _DropZone(QFrame):
    """拖拽落区。"""

    folderDropped = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("docsorterDropZone")
        self.setAcceptDrops(True)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(SPACE_SM)
        layout.addWidget(
            StrongBodyLabel("把文件夹拖到这里", self),
            alignment=Qt.AlignmentFlag.AlignHCenter,
        )
        layout.addWidget(
            CaptionLabel("或者点下面的按钮浏览", self),
            alignment=Qt.AlignmentFlag.AlignHCenter,
        )

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if self._first_local_dir(event) is not None:
            event.acceptProposedAction()
            self.setObjectName("docsorterDropZoneActive")
            self.setStyleSheet(self.styleSheet())
        else:
            event.ignore()

    def dragLeaveEvent(self, event: object) -> None:  # noqa: N802, ARG002
        self.setObjectName("docsorterDropZone")
        self.setStyleSheet(self.styleSheet())

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        folder = self._first_local_dir(event)
        self.setObjectName("docsorterDropZone")
        self.setStyleSheet(self.styleSheet())
        if folder is None:
            event.ignore()
            return
        event.acceptProposedAction()
        self.folderDropped.emit(folder)

    @staticmethod
    def _first_local_dir(event: object) -> Path | None:
        mime = getattr(event, "mimeData", None)
        if mime is None:
            return None
        data = mime()
        if not data.hasUrls():
            return None
        for url in data.urls():
            if not url.isLocalFile():
                continue
            candidate = Path(url.toLocalFile())
            if candidate.is_dir():
                return candidate
        return None


class SelectPage(QWidget):
    """第一步：选择要整理的目录。"""

    #: (root, options)
    scanRequested = Signal(object, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("selectPage")
        self._root: Path | None = None
        self._recent: list[str] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACE_XL, SPACE_XL, SPACE_XL, SPACE_XL)
        outer.setSpacing(SPACE_LG)

        outer.addWidget(TitleLabel("选择要整理的目录", self))
        outer.addWidget(
            CaptionLabel(
                "默认只整理该目录下散落的文件。目录里的子文件夹保持原样，"
                "需要一起整理的话下一步可以逐个勾选。",
                self,
            )
        )

        self._drop = _DropZone(self)
        self._drop.folderDropped.connect(self._set_root)
        outer.addWidget(self._drop)

        browse_row = QHBoxLayout()
        browse_row.setSpacing(SPACE_SM)
        self._browse = PushButton("浏览…", self)
        self._browse.clicked.connect(self._on_browse)
        browse_row.addWidget(self._browse)
        browse_row.addStretch(1)
        outer.addLayout(browse_row)

        self._path_label = BodyLabel("尚未选择目录", self)
        self._path_label.setWordWrap(True)
        outer.addWidget(self._path_label)

        self._reason_label = CaptionLabel("", self)
        self._reason_label.setWordWrap(True)
        self._reason_label.setStyleSheet(f"color: {DANGER};")
        self._reason_label.setVisible(False)
        outer.addWidget(self._reason_label)

        outer.addWidget(SubtitleLabel("最近使用", self))
        self._recent_row = QVBoxLayout()
        self._recent_row.setSpacing(SPACE_XS)
        outer.addLayout(self._recent_row)

        outer.addWidget(SubtitleLabel("高级选项", self))
        self._include_hidden = CheckBox("包含隐藏文件", self)
        self._follow_symlinks = CheckBox("跟随符号链接与 junction（不建议）", self)
        outer.addWidget(self._include_hidden)
        outer.addWidget(self._follow_symlinks)

        outer.addStretch(1)

        action_row = QHBoxLayout()
        action_row.addStretch(1)
        self._scan = PrimaryPushButton("开始扫描", self)
        self._scan.setEnabled(False)
        self._scan.clicked.connect(self._emit_scan)
        action_row.addWidget(self._scan)
        outer.addLayout(action_row)
        outer.setSpacing(SPACE_MD)

    # -- 对外 -------------------------------------------------------------

    @property
    def root(self) -> Path | None:
        return self._root

    def options(self) -> ScanOptions:
        return ScanOptions(
            include_hidden=self._include_hidden.isChecked(),
            follow_symlinks=self._follow_symlinks.isChecked(),
        )

    def set_recent(self, roots: list[str]) -> None:
        self._recent = list(roots)[:_MAX_RECENT]
        while self._recent_row.count():
            item = self._recent_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if not self._recent:
            self._recent_row.addWidget(CaptionLabel("暂无记录", self))
            return
        for entry in self._recent:
            button = PushButton(entry, self)
            button.clicked.connect(lambda _=False, e=entry: self._set_root(Path(e)))
            self._recent_row.addWidget(button)

    def show_admission(self, result: AdmissionResult) -> None:
        """展示准入结果。需求 1.8。"""
        if result.allowed:
            self._reason_label.setVisible(False)
            self._scan.setEnabled(True)
            return
        self._reason_label.setText(result.message)
        self._reason_label.setVisible(True)
        self._scan.setEnabled(False)

    def set_busy(self, busy: bool) -> None:
        self._scan.setEnabled(not busy and self._root is not None)
        self._browse.setEnabled(not busy)

    # -- 内部 -------------------------------------------------------------

    def _on_browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择要整理的目录")
        if chosen:
            self._set_root(Path(chosen))

    def _set_root(self, folder: Path) -> None:
        self._root = folder
        self._path_label.setText(str(folder))
        self._reason_label.setVisible(False)
        self._scan.setEnabled(True)

    def _emit_scan(self) -> None:
        if self._root is not None:
            self.scanRequested.emit(self._root, self.options())
