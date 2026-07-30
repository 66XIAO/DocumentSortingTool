"""预览树的筛选代理。需求 10.12。

按行索引白名单过滤，不复制数据——``PlanTreeModel`` 的节点已经只存下标，再复制一份
就把省下来的内存又花掉了。

类目行的可见性由其子行决定：一个类目下没有任何行通过筛选时，类目本身也隐藏，
否则会看到一堆空类目。
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QObject, QSortFilterProxyModel, Qt

from app.core.models import ConflictKind
from app.ui.widgets.plan_tree_model import ROLE_IS_GROUP, Column, PlanTreeModel


class PlanFilterProxy(QSortFilterProxyModel):
    """名称搜索 + 只看冲突 + 只看未分类。"""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._query = ""
        self._only_conflicts = False
        self._only_unclassified = False
        self.setRecursiveFilteringEnabled(True)

    # -- 筛选条件 ---------------------------------------------------------

    def set_query(self, text: str) -> None:
        self._query = text.strip().lower()
        self._refresh()

    def set_only_conflicts(self, enabled: bool) -> None:
        self._only_conflicts = enabled
        self._refresh()

    def set_only_unclassified(self, enabled: bool) -> None:
        self._only_unclassified = enabled
        self._refresh()

    def _refresh(self) -> None:
        """重新求值筛选。

        用公开槽 ``invalidate()`` 而非 ``invalidateFilter`` / ``invalidateRowsFilter``：
        这个 PySide6 版本把后两个都标了废弃（它们是保护级虚函数）。``invalidate``
        会连排序一起失效，但本代理不排序，没有代价。
        """
        self.invalidate()

    @property
    def active(self) -> bool:
        return bool(self._query) or self._only_conflicts or self._only_unclassified

    # -- 判定 -------------------------------------------------------------

    def filterAcceptsRow(  # noqa: N802
        self, source_row: int, source_parent: QModelIndex
    ) -> bool:
        if not self.active:
            return True

        model = self.sourceModel()
        if not isinstance(model, PlanTreeModel):
            return True

        index = model.index(source_row, Column.NAME, source_parent)
        if not index.isValid():
            return True

        if index.data(ROLE_IS_GROUP):
            # 类目行交给 recursiveFiltering 按子行决定，但子行可能还没 fetch。
            # 未加载的类目一律先放行，展开后再由子行自行过滤——否则用户会以为
            # 筛选把整个类目都排除了。
            return True

        item = model.item_at(index)
        if item is None:
            return True

        if self._only_conflicts and item.conflict is ConflictKind.NONE:
            return False
        if self._only_unclassified and not _is_unclassified(model, item):
            return False
        if self._query and self._query not in item.entry.name.lower():
            return False
        return True


def _is_unclassified(model: PlanTreeModel, item: object) -> bool:
    plan = model.plan
    if plan is None:
        return False
    return any(u.entry.path == item.entry.path for u in plan.unclassified)  # type: ignore[attr-defined]
