"""子文件夹选择器。需求 2.6-2.8。

树形逐级展开，每个子文件夹一个独立复选框。语义上最关键的一点：**勾选不向下继承**
（需求 2.7）。所以这里刻意**不用** Qt 的三态复选框——三态的视觉语义是「部分子项被
选中」，会让用户以为勾父就等于勾了一批子，而实际语义是「只作用于该文件夹的散落
文件」。每个节点的勾选状态彼此独立，视觉上也就该彼此独立。

首次勾选时弹一次后果说明（需求 2.8），说明勾选会把文件移出原文件夹。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTreeWidgetItem, QVBoxLayout, QWidget

from app.core.models import SubfolderInfo
from app.ui.theme import (
    SPACE_MD,
    SPACE_SM,
    CaptionLabel,
    StrongBodyLabel,
    TreeWidget,
    confirm,
)
from app.ui.theme.tokens import SUBFOLDER_PICK_CONSEQUENCE

_PATH_ROLE = Qt.ItemDataRole.UserRole
_LOADED_ROLE = Qt.ItemDataRole.UserRole + 1

COLUMN_NAME = 0
COLUMN_LOOSE = 1
COLUMN_TOTAL = 2
COLUMN_SIZE = 3


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class SubfolderPicker(QWidget):
    """子文件夹清单与勾选。"""

    #: (folder, selected)
    selectionToggled = Signal(object, bool)
    #: 用户展开了一个尚未加载子节点的文件夹
    expandRequested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._suppress = False
        self._consequence_shown = False
        self._items: dict[Path, QTreeWidgetItem] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_SM)

        self._title = StrongBodyLabel("子文件夹", self)
        layout.addWidget(self._title)

        self._hint = CaptionLabel(
            "默认只整理根目录下的散落文件。需要连子文件夹里的文件一起整理时，"
            "在下面逐个勾选——勾选不会自动包含它的下级。",
            self,
        )
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)

        self._tree = TreeWidget(self)
        self._tree.setColumnCount(4)
        self._tree.setHeaderLabels(["文件夹", "散落文件", "全部文件", "占用空间"])
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._tree.setUniformRowHeights(True)
        header = self._tree.header()
        header.setSectionResizeMode(COLUMN_NAME, QHeaderView.ResizeMode.Stretch)
        for column in (COLUMN_LOOSE, COLUMN_TOTAL, COLUMN_SIZE):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self._tree, stretch=1)
        layout.setContentsMargins(0, 0, 0, SPACE_MD)

        self._tree.itemChanged.connect(self._on_item_changed)
        self._tree.itemExpanded.connect(self._on_item_expanded)

    # -- 填充 -------------------------------------------------------------

    def set_root_subfolders(self, infos: list[SubfolderInfo]) -> None:
        self._suppress = True
        try:
            self._tree.clear()
            self._items.clear()
            for info in infos:
                item = self._make_item(info)
                self._tree.addTopLevelItem(item)
        finally:
            self._suppress = False

    def set_children(self, parent_folder: Path, infos: list[SubfolderInfo]) -> None:
        parent_item = self._items.get(Path(parent_folder))
        if parent_item is None:
            return
        self._suppress = True
        try:
            parent_item.takeChildren()
            for info in infos:
                parent_item.addChild(self._make_item(info))
            parent_item.setData(COLUMN_NAME, _LOADED_ROLE, True)
        finally:
            self._suppress = False

    def _make_item(self, info: SubfolderInfo) -> QTreeWidgetItem:
        item = QTreeWidgetItem(
            [
                info.name,
                str(info.loose_file_count),
                str(info.recursive_file_count),
                format_size(info.recursive_size),
            ]
        )
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(
            COLUMN_NAME,
            Qt.CheckState.Checked if info.selected else Qt.CheckState.Unchecked,
        )
        item.setData(COLUMN_NAME, _PATH_ROLE, str(info.path))
        item.setData(COLUMN_NAME, _LOADED_ROLE, not info.has_children)
        item.setToolTip(COLUMN_NAME, str(info.path))
        item.setToolTip(
            COLUMN_LOOSE, "该文件夹下直接散落的文件数，勾选后参与整理的就是这些"
        )
        item.setToolTip(
            COLUMN_TOTAL, "含所有下级的文件总数，仅供参考，不代表会被整理"
        )
        if info.has_children:
            # 放一个占位子项，用户点开时才真正去读盘
            placeholder = QTreeWidgetItem(["载入中…", "", "", ""])
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            item.addChild(placeholder)
        self._items[info.path] = item
        return item

    # -- 交互 -------------------------------------------------------------

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._suppress or column != COLUMN_NAME:
            return
        raw = item.data(COLUMN_NAME, _PATH_ROLE)
        if not raw:
            return
        checked = item.checkState(COLUMN_NAME) == Qt.CheckState.Checked

        if checked and not self._consequence_shown:
            accepted = confirm(
                self,
                "勾选后这些文件会被移出原文件夹",
                SUBFOLDER_PICK_CONSEQUENCE,
                ok_text="我知道了，继续",
            )
            self._consequence_shown = True
            if not accepted:
                self._suppress = True
                try:
                    item.setCheckState(COLUMN_NAME, Qt.CheckState.Unchecked)
                finally:
                    self._suppress = False
                return

        self.selectionToggled.emit(Path(raw), checked)

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        if item.data(COLUMN_NAME, _LOADED_ROLE):
            return
        raw = item.data(COLUMN_NAME, _PATH_ROLE)
        if raw:
            self.expandRequested.emit(Path(raw))

    # -- 状态同步 ---------------------------------------------------------

    def set_checked(self, folder: Path, checked: bool) -> None:
        """外部纠正勾选状态（例如补扫被取消，需要把复选框弹回去）。"""
        item = self._items.get(Path(folder))
        if item is None:
            return
        self._suppress = True
        try:
            item.setCheckState(
                COLUMN_NAME,
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked,
            )
        finally:
            self._suppress = False

    def checked_folders(self) -> list[Path]:
        return [
            path
            for path, item in self._items.items()
            if item.checkState(COLUMN_NAME) == Qt.CheckState.Checked
        ]

    def set_enabled_for_scan(self, enabled: bool) -> None:
        self._tree.setEnabled(enabled)
