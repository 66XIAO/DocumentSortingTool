"""扫描分析页。需求 2.6-2.8、17.9-17.11。

三种状态互斥地占据同一块区域：骨架屏（扫描中）、错误态（扫描失败）、结果视图
（统计摘要 + 子文件夹选择器）。用 QStackedWidget 而非显隐控制，避免出现两种状态
同时可见的中间态。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.core.models import SubfolderInfo
from app.core.progress import ProgressSnapshot
from app.core.scanner import ScanResult
from app.ui.theme import (
    SPACE_LG,
    SPACE_MD,
    SPACE_SM,
    SPACE_XL,
    CaptionLabel,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    TitleLabel,
)
from app.ui.widgets.states import EmptyState, ErrorState, SkeletonState
from app.ui.widgets.subfolder_picker import SubfolderPicker, format_size

PAGE_SKELETON = 0
PAGE_RESULT = 1
PAGE_ERROR = 2
PAGE_EMPTY = 3


class _StatTile(QWidget):
    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_SM)
        self._value = StrongBodyLabel("—", self)
        self._value.setObjectName("statValue")
        self._label = CaptionLabel(label, self)
        self._label.setObjectName("statLabel")
        layout.addWidget(self._value)
        layout.addWidget(self._label)

    def set_value(self, text: str) -> None:
        self._value.setText(text)


class ScanPage(QWidget):
    """第二步：扫描分析与扫描范围勾选。"""

    #: (folder, selected)
    selectionToggled = Signal(object, bool)
    expandRequested = Signal(object)
    retryRequested = Signal()
    proceedRequested = Signal()
    cancelRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("scanPage")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACE_XL, SPACE_XL, SPACE_XL, SPACE_XL)
        outer.setSpacing(SPACE_MD)

        outer.addWidget(TitleLabel("扫描分析", self))
        self._root_label = CaptionLabel("", self)
        self._root_label.setWordWrap(True)
        outer.addWidget(self._root_label)

        self._stack = QStackedWidget(self)
        outer.addWidget(self._stack, stretch=1)

        # 0 骨架屏
        self._skeleton = SkeletonState("正在扫描目录…", self)
        self._stack.addWidget(self._skeleton)

        # 1 结果
        self._result_view = QWidget(self)
        result_layout = QVBoxLayout(self._result_view)
        result_layout.setContentsMargins(0, 0, 0, 0)
        result_layout.setSpacing(SPACE_LG)

        stats = QGridLayout()
        stats.setHorizontalSpacing(SPACE_XL)
        self._tile_loose = _StatTile("待整理的散落文件", self._result_view)
        self._tile_subfolders = _StatTile("子文件夹", self._result_view)
        self._tile_selected = _StatTile("已勾选参与整理", self._result_view)
        self._tile_errors = _StatTile("读取失败", self._result_view)
        for column, tile in enumerate(
            (self._tile_loose, self._tile_subfolders, self._tile_selected, self._tile_errors)
        ):
            stats.addWidget(tile, 0, column)
        stats.setColumnStretch(4, 1)
        result_layout.addLayout(stats)

        self._picker = SubfolderPicker(self._result_view)
        self._picker.selectionToggled.connect(self.selectionToggled)
        self._picker.expandRequested.connect(self.expandRequested)
        result_layout.addWidget(self._picker, stretch=1)

        self._stack.addWidget(self._result_view)

        # 2 错误
        self._error = ErrorState(
            "扫描失败", "", "重试", lambda: self.retryRequested.emit(), self
        )
        self._stack.addWidget(self._error)

        # 3 空态
        self._empty = EmptyState(
            "这个目录下没有散落的文件",
            "所有内容都在子文件夹里。可以在上面勾选需要一起整理的子文件夹，"
            "或者返回上一步换一个目录。",
            parent=self,
        )
        self._stack.addWidget(self._empty)

        action_row = QHBoxLayout()
        action_row.setSpacing(SPACE_SM)
        self._cancel = PushButton("停止扫描", self)
        self._cancel.clicked.connect(lambda: self.cancelRequested.emit())
        action_row.addWidget(self._cancel)
        action_row.addStretch(1)
        self._proceed = PrimaryPushButton("生成分类方案", self)
        self._proceed.setEnabled(False)
        self._proceed.clicked.connect(lambda: self.proceedRequested.emit())
        action_row.addWidget(self._proceed)
        outer.addLayout(action_row)

        self.show_scanning()

    # -- 状态切换 ---------------------------------------------------------

    def set_root(self, root: Path) -> None:
        self._root_label.setText(f"整理根目录：{root}")

    def show_scanning(self, message: str = "正在扫描目录…") -> None:
        self._skeleton.set_message(message)
        self._skeleton.set_detail("")
        self._stack.setCurrentIndex(PAGE_SKELETON)
        self._cancel.setEnabled(True)
        self._proceed.setEnabled(False)

    def show_progress(self, snapshot: ProgressSnapshot) -> None:
        detail = f"已处理 {snapshot.processed:,} 项"
        if snapshot.current:
            detail += f" · {snapshot.current}"
        self._skeleton.set_detail(detail)

    def show_error(self, detail: str) -> None:
        self._error.set_error("扫描失败", detail)
        self._stack.setCurrentIndex(PAGE_ERROR)
        self._cancel.setEnabled(False)
        self._proceed.setEnabled(False)

    def show_result(self, result: ScanResult) -> None:
        self._tile_loose.set_value(f"{len(result.entries):,}")
        self._tile_subfolders.set_value(f"{len(result.subfolders):,}")
        self._tile_selected.set_value("0")
        self._tile_errors.set_value(f"{len(result.errors):,}")
        self._picker.set_root_subfolders(result.subfolders)
        self._cancel.setEnabled(False)
        self._proceed.setEnabled(bool(result.entries))

        if not result.entries and not result.subfolders:
            self._empty.set_text(
                "这个目录是空的",
                "没有可整理的文件。返回上一步换一个目录试试。",
            )
            self._stack.setCurrentIndex(PAGE_EMPTY)
        elif not result.entries:
            self._stack.setCurrentIndex(PAGE_RESULT)
        else:
            self._stack.setCurrentIndex(PAGE_RESULT)

    def update_counts(self, entry_count: int, selected_count: int) -> None:
        self._tile_loose.set_value(f"{entry_count:,}")
        self._tile_selected.set_value(f"{selected_count:,}")
        self._proceed.setEnabled(entry_count > 0)

    def set_children(self, folder: Path, infos: list[SubfolderInfo]) -> None:
        self._picker.set_children(folder, infos)

    def revert_checkbox(self, folder: Path, checked: bool) -> None:
        """补扫被取消或被拒绝时把复选框弹回去。"""
        self._picker.set_checked(folder, checked)

    def set_busy(self, busy: bool) -> None:
        self._picker.set_enabled_for_scan(not busy)
        self._cancel.setEnabled(busy)


__all__ = ["ScanPage", "format_size"]
