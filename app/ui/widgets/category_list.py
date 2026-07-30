"""类目列表与编辑动作。需求 10.2、10.3。

改名 / 换色 / 合并 / 新建 / 删除都只发信号，不自己改方案——所有修改必须经
``PlanService`` 记入 override 集合再重算，否则「切换策略后手工调整还在不在」就
取决于哪条路径先跑（需求 19.3）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QColorDialog,
    QHBoxLayout,
    QInputDialog,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.models import Category, SortPlan
from app.ui.theme import (
    SPACE_SM,
    SPACE_XS,
    CaptionLabel,
    PushButton,
    StrongBodyLabel,
    confirm,
)

_PARTS_ROLE = Qt.ItemDataRole.UserRole


def _swatch(color: str, size: int = 12) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(0, 0, size - 1, size - 1, 3, 3)
    painter.end()
    return pixmap


class CategoryList(QWidget):
    """左栏：类目列表。"""

    renameRequested = Signal(object, object)  # (原 parts, 新 parts)
    recolorRequested = Signal(object, str)
    mergeRequested = Signal(object, object)  # (源 parts, 目标 parts)
    createRequested = Signal(object)
    deleteRequested = Signal(object)
    categorySelected = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._counts: dict[tuple[str, ...], int] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_SM)

        layout.addWidget(StrongBodyLabel("类目", self))
        self._hint = CaptionLabel("双击改名，右键更多操作", self)
        layout.addWidget(self._hint)

        self._list = QListWidget(self)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.itemDoubleClicked.connect(self._on_rename)
        self._list.currentItemChanged.connect(self._on_current_changed)
        layout.addWidget(self._list, stretch=1)

        buttons = QHBoxLayout()
        buttons.setSpacing(SPACE_XS)
        self._new = PushButton("新建", self)
        self._recolor = PushButton("换色", self)
        self._merge = PushButton("合并", self)
        self._delete = PushButton("删除", self)
        for button in (self._new, self._recolor, self._merge, self._delete):
            buttons.addWidget(button)
        layout.addLayout(buttons)

        self._new.clicked.connect(self._on_create)
        self._recolor.clicked.connect(self._on_recolor)
        self._merge.clicked.connect(self._on_merge)
        self._delete.clicked.connect(self._on_delete)
        self._update_enabled()

    # -- 填充 -------------------------------------------------------------

    def set_plan(self, plan: SortPlan | None) -> None:
        self._list.clear()
        self._counts.clear()
        if plan is None:
            self._update_enabled()
            return

        for item in plan.all_items():
            category = plan.category_by_id(item.category_id)
            if category is not None:
                self._counts[category.path_parts] = (
                    self._counts.get(category.path_parts, 0) + 1
                )

        for category in plan.categories:
            self._list.addItem(self._make_item(category))
        self._update_enabled()

    def _make_item(self, category: Category) -> QListWidgetItem:
        count = self._counts.get(category.path_parts, 0)
        label = "/".join(category.path_parts)
        item = QListWidgetItem(f"{label}   （{count}）")
        item.setIcon(_swatch(category.color))
        item.setData(_PARTS_ROLE, list(category.path_parts))
        item.setToolTip(f"{label}\n{count} 个文件\n来源：{category.rule_source}")
        return item

    # -- 选中 -------------------------------------------------------------

    def current_parts(self) -> tuple[str, ...] | None:
        item = self._list.currentItem()
        if item is None:
            return None
        raw = item.data(_PARTS_ROLE)
        return tuple(raw) if raw else None

    def all_parts(self) -> list[tuple[str, ...]]:
        result: list[tuple[str, ...]] = []
        for row in range(self._list.count()):
            raw = self._list.item(row).data(_PARTS_ROLE)
            if raw:
                result.append(tuple(raw))
        return result

    def _on_current_changed(self) -> None:
        self._update_enabled()
        parts = self.current_parts()
        if parts is not None:
            self.categorySelected.emit(parts)

    def _update_enabled(self) -> None:
        has = self.current_parts() is not None
        self._recolor.setEnabled(has)
        self._delete.setEnabled(has)
        self._merge.setEnabled(has and self._list.count() > 1)

    # -- 动作 -------------------------------------------------------------

    def _on_rename(self) -> None:
        parts = self.current_parts()
        if parts is None:
            return
        current = "/".join(parts)
        text, ok = QInputDialog.getText(
            self, "重命名类目", "用 / 分隔层级：", text=current
        )
        if not ok:
            return
        new_parts = tuple(seg for seg in text.split("/") if seg.strip())
        if not new_parts or new_parts == parts:
            return
        self.renameRequested.emit(parts, new_parts)

    def _on_recolor(self) -> None:
        parts = self.current_parts()
        if parts is None:
            return
        color = QColorDialog.getColor(parent=self, title="选择类目颜色")
        if color.isValid():
            self.recolorRequested.emit(parts, color.name())

    def _on_merge(self) -> None:
        source = self.current_parts()
        if source is None:
            return
        others = [p for p in self.all_parts() if p != source]
        if not others:
            return
        labels = ["/".join(p) for p in others]
        choice, ok = QInputDialog.getItem(
            self, "合并类目", f"把「{'/'.join(source)}」并入：", labels, 0, False
        )
        if ok and choice:
            self.mergeRequested.emit(source, others[labels.index(choice)])

    def _on_create(self) -> None:
        text, ok = QInputDialog.getText(self, "新建类目", "用 / 分隔层级：")
        if not ok:
            return
        parts = tuple(seg for seg in text.split("/") if seg.strip())
        if parts:
            self.createRequested.emit(parts)

    def _on_delete(self) -> None:
        parts = self.current_parts()
        if parts is None:
            return
        label = "/".join(parts)
        count = self._counts.get(parts, 0)
        if not confirm(
            self,
            f"删除类目「{label}」",
            f"该类目下的 {count} 个文件会退回「_未分类」，不会被删除。\n"
            "这只是调整方案，磁盘上的文件此刻不受影响。",
            ok_text="删除类目",
        ):
            return
        self.deleteRequested.emit(parts)
