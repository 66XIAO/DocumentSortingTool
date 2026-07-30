"""方案预览树的模型。需求 10.4-10.6、10.13、10.14。

## 为什么不用 QTreeWidget

10 万条目下 `QTreeWidget` 要构造 10 万个 `QTreeWidgetItem`，对象开销与构建时间都
过不了 100ms 响应的要求（需求 10.14）。这里用 `QAbstractItemModel`：

- 内部只维护 ``list[_Node]``，节点是 ``slots`` dataclass，文件行的实际数据留在
  ``SortPlan.items`` 的扁平数组里，节点只存下标。
- 类目节点实现 ``canFetchMore`` / ``fetchMore``，每次追加 ``FETCH_BATCH`` 个子行，
  因此展开一个 5 万文件的类目也只构造一批节点。
- 排序在构建时一次完成，滚动过程零排序。

## 整理前 / 整理后是两个分组器共享同一份数据

切换只换分组键——整理前按 ``entry.path.parent`` 分组、整理后按 ``category_id``
分组——不重建 ``items``（需求 10.4）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractItemModel, QModelIndex, QObject, Qt

from app.core.models import ActionKind, ConflictKind, PlanItem, SortPlan
from app.ui.theme import CONFLICT, SUCCESS, category_color

#: 每次 fetchMore 追加的子行数
FETCH_BATCH = 200


class Column(IntEnum):
    NAME = 0
    ACTION = 1
    REASON = 2


HEADERS: tuple[str, ...] = ("名称", "动作", "理由")


class ViewMode(IntEnum):
    AFTER = 0  # 整理后：按类目分组
    BEFORE = 1  # 整理前：按原目录分组


#: 自定义角色
ROLE_ITEM_INDEX = int(Qt.ItemDataRole.UserRole) + 1
ROLE_IS_GROUP = int(Qt.ItemDataRole.UserRole) + 2
ROLE_GROUP_KEY = int(Qt.ItemDataRole.UserRole) + 3


@dataclass(slots=True)
class _Node:
    """树节点。

    ``item_index`` 是 ``SortPlan`` 扁平数组里的下标，不是数据本身——这是把 10 万
    行的内存开销压下来的关键。
    """

    label: str
    parent_row: int | None
    is_group: bool
    item_index: int = -1
    color: str = ""
    children: list[int] = field(default_factory=list)
    loaded: int = 0
    total: int = 0
    pending: list[int] = field(default_factory=list)


class PlanTreeModel(QAbstractItemModel):
    """目标结构树。"""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._plan: SortPlan | None = None
        self._items: list[PlanItem] = []
        self._nodes: list[_Node] = []
        self._roots: list[int] = []
        self._mode = ViewMode.AFTER

    # -- 数据装载 ---------------------------------------------------------

    def set_plan(self, plan: SortPlan | None) -> None:
        self.beginResetModel()
        self._plan = plan
        self._items = plan.all_items() if plan else []
        self._rebuild()
        self.endResetModel()

    def set_mode(self, mode: ViewMode) -> None:
        """切换整理前 / 整理后。只换分组键，不重建数据。需求 10.4。"""
        if mode == self._mode:
            return
        self.beginResetModel()
        self._mode = mode
        self._rebuild()
        self.endResetModel()

    @property
    def mode(self) -> ViewMode:
        return self._mode

    @property
    def plan(self) -> SortPlan | None:
        return self._plan

    def item_at(self, index: QModelIndex) -> PlanItem | None:
        if not index.isValid():
            return None
        node = self._nodes[index.internalId()]
        if node.is_group or node.item_index < 0:
            return None
        return self._items[node.item_index]

    def group_key_at(self, index: QModelIndex) -> str | None:
        if not index.isValid():
            return None
        node = self._nodes[index.internalId()]
        return node.label if node.is_group else None

    # -- 构建 -------------------------------------------------------------

    def _rebuild(self) -> None:
        self._nodes = []
        self._roots = []
        if not self._items:
            return

        groups: dict[str, list[int]] = {}
        colors: dict[str, str] = {}

        for position, item in enumerate(self._items):
            key, color = self._group_of(item, position)
            groups.setdefault(key, []).append(position)
            colors.setdefault(key, color)

        for order, key in enumerate(sorted(groups)):
            members = sorted(groups[key], key=lambda i: self._items[i].entry.name)
            node = _Node(
                label=key,
                parent_row=None,
                is_group=True,
                color=colors.get(key) or category_color(order),
                total=len(members),
                pending=members,
            )
            self._nodes.append(node)
            self._roots.append(len(self._nodes) - 1)

    def _group_of(self, item: PlanItem, position: int) -> tuple[str, str]:
        if self._mode is ViewMode.BEFORE:
            return str(item.entry.path.parent), ""
        plan = self._plan
        category = plan.category_by_id(item.category_id) if plan else None
        if category is None:
            return "?", ""
        return "/".join(category.path_parts), category.color

    # -- QAbstractItemModel ----------------------------------------------

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802, ARG002
        return len(HEADERS)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if not parent.isValid():
            return len(self._roots)
        node = self._nodes[parent.internalId()]
        return len(node.children) if node.is_group else 0

    def index(  # noqa: N802
        self, row: int, column: int, parent: QModelIndex = QModelIndex()
    ) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        if not parent.isValid():
            return self.createIndex(row, column, self._roots[row])
        node = self._nodes[parent.internalId()]
        return self.createIndex(row, column, node.children[row])

    def parent(self, index: QModelIndex) -> QModelIndex:  # noqa: N802
        if not index.isValid():
            return QModelIndex()
        node = self._nodes[index.internalId()]
        if node.parent_row is None:
            return QModelIndex()
        parent_node_id = node.parent_row
        try:
            row = self._roots.index(parent_node_id)
        except ValueError:
            return QModelIndex()
        return self.createIndex(row, 0, parent_node_id)

    def hasChildren(  # noqa: N802
        self, parent: QModelIndex = QModelIndex()
    ) -> bool:
        if not parent.isValid():
            return bool(self._roots)
        node = self._nodes[parent.internalId()]
        return node.is_group and node.total > 0

    def canFetchMore(self, parent: QModelIndex) -> bool:  # noqa: N802
        if not parent.isValid():
            return False
        node = self._nodes[parent.internalId()]
        return node.is_group and bool(node.pending)

    def fetchMore(self, parent: QModelIndex) -> None:  # noqa: N802
        if not parent.isValid():
            return
        node_id = parent.internalId()
        node = self._nodes[node_id]
        if not node.is_group or not node.pending:
            return

        batch = node.pending[:FETCH_BATCH]
        node.pending = node.pending[FETCH_BATCH:]
        start = len(node.children)

        self.beginInsertRows(parent, start, start + len(batch) - 1)
        for item_index in batch:
            child = _Node(
                label=self._items[item_index].entry.name,
                parent_row=node_id,
                is_group=False,
                item_index=item_index,
            )
            self._nodes.append(child)
            node.children.append(len(self._nodes) - 1)
        node.loaded = len(node.children)
        self.endInsertRows()

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if (
            orientation is Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
            and 0 <= section < len(HEADERS)
        ):
            return HEADERS[section]
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        node = self._nodes[index.internalId()]
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if node.is_group:
            return base | Qt.ItemFlag.ItemIsDropEnabled
        if index.column() == Column.NAME:
            # 复选框用于批量排除（需求 10.10），拖拽用于改类目（需求 10.11）
            return (
                base
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsDragEnabled
            )
        return base

    def data(  # noqa: C901
        self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if not index.isValid():
            return None
        node = self._nodes[index.internalId()]
        column = index.column()

        if role == ROLE_IS_GROUP:
            return node.is_group
        if role == ROLE_ITEM_INDEX:
            return node.item_index
        if role == ROLE_GROUP_KEY:
            return node.label if node.is_group else None

        if node.is_group:
            return self._group_data(node, column, role)
        return self._item_data(node, column, role)

    def _group_data(self, node: _Node, column: int, role: int) -> Any:
        if role == Qt.ItemDataRole.DisplayRole:
            if column == Column.NAME:
                return f"{node.label}/"
            if column == Column.ACTION:
                return f"{node.total} 个文件"
            return None
        if role == Qt.ItemDataRole.ForegroundRole and column == Column.NAME:
            from PySide6.QtGui import QColor

            return QColor(node.color) if node.color else None
        if role == Qt.ItemDataRole.FontRole and column == Column.NAME:
            from PySide6.QtGui import QFont

            font = QFont()
            font.setBold(True)
            return font
        return None

    def _item_data(self, node: _Node, column: int, role: int) -> Any:
        item = self._items[node.item_index]

        if role == Qt.ItemDataRole.DisplayRole:
            if column == Column.NAME:
                # 需求 10.6：目标名与源名不同时显示「旧名 → 新名」
                if item.renamed_from:
                    return f"{item.renamed_from} → {item.target.name}"
                return item.entry.name
            if column == Column.ACTION:
                return _action_label(item)
            if column == Column.REASON:
                return item.reason
            return None

        if role == Qt.ItemDataRole.CheckStateRole and column == Column.NAME:
            return (
                Qt.CheckState.Checked if item.included else Qt.CheckState.Unchecked
            )

        if role == Qt.ItemDataRole.ToolTipRole:
            return (
                f"原路径：{item.entry.path}\n"
                f"目标：{item.target}\n"
                f"理由：{item.reason}"
            )

        if role == Qt.ItemDataRole.DecorationRole and column == Column.NAME:
            # 需求 10.5：冲突项显示橙色角标
            if item.conflict is not ConflictKind.NONE:
                return _dot(CONFLICT)
            if item.by_llm:
                return _dot(SUCCESS)
            return None

        if role == Qt.ItemDataRole.ForegroundRole and column == Column.ACTION:
            from PySide6.QtGui import QColor

            if item.conflict is not ConflictKind.NONE:
                return QColor(CONFLICT)
            return None

        return None

    def setData(  # noqa: N802
        self, index: QModelIndex, value: Any, role: int = Qt.ItemDataRole.EditRole
    ) -> bool:
        if not index.isValid() or role != Qt.ItemDataRole.CheckStateRole:
            return False
        node = self._nodes[index.internalId()]
        if node.is_group or node.item_index < 0:
            return False
        item = self._items[node.item_index]
        item.included = Qt.CheckState(value) == Qt.CheckState.Checked
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        return True

    # -- 辅助 -------------------------------------------------------------

    def visible_item_indexes(self) -> Sequence[int]:
        return [n.item_index for n in self._nodes if not n.is_group]

    def group_labels(self) -> list[str]:
        return [self._nodes[i].label for i in self._roots]


def _action_label(item: PlanItem) -> str:
    parts: list[str] = []
    if item.action is ActionKind.SKIP:
        parts.append("跳过")
    elif item.action is ActionKind.COPY:
        parts.append("复制")
    else:
        parts.append("移动")
    if item.conflict is not ConflictKind.NONE:
        parts.append(_CONFLICT_LABELS.get(item.conflict, str(item.conflict)))
    if not item.included:
        parts.append("未勾选")
    return " · ".join(parts)


_CONFLICT_LABELS: dict[ConflictKind, str] = {
    ConflictKind.EXISTS: "目标已存在",
    ConflictKind.PATH_TOO_LONG: "路径过长",
    ConflictKind.LOCKED: "文件被占用",
    ConflictKind.PATH_ESCAPE: "越出根目录",
}


def _dot(color: str) -> Any:
    """一个纯色小圆点，用作状态角标。"""
    from PySide6.QtGui import QColor, QPainter, QPixmap

    size = 10
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(0, 0, size - 1, size - 1)
    painter.end()
    return pixmap
