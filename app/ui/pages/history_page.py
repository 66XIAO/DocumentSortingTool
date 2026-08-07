"""历史记录页。需求 15。

不是「锦上添花的日志页」：需求 15.2 要求任意一条未撤销的 run 都能撤销。用户可能几天
后才发现某次整理有问题，那时唯一的退路就是这一页。
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.history import RunSummary
from app.core.models import RunStatus, Strategy
from app.ui.theme import (
    DANGER,
    SPACE_LG,
    SPACE_MD,
    SPACE_SM,
    SPACE_XL,
    WARNING,
    CaptionLabel,
    PrimaryPushButton,
    PushButton,
    TitleLabel,
    confirm,
)
from app.ui.widgets.states import EmptyState

_RUN_ROLE = Qt.ItemDataRole.UserRole

_STATUS_LABELS: dict[RunStatus, str] = {
    RunStatus.COMPLETED: "已完成",
    RunStatus.FAILED: "部分失败",
    RunStatus.UNDONE: "已撤销",
    RunStatus.UNFINISHED: "未收尾",
    RunStatus.RUNNING: "进行中",
}

_STRATEGY_LABELS: dict[Strategy, str] = {
    Strategy.BY_TYPE: "按类型",
    Strategy.BY_DATE: "按时间",
    Strategy.TYPE_AND_DATE: "类型+时间",
    Strategy.SMART: "智能",
}


class HistoryPage(QWidget):
    """整理历史时间轴。"""

    undoRequested = Signal(str)
    refreshRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("historyPage")
        self._summaries: dict[str, RunSummary] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACE_XL, SPACE_LG, SPACE_XL, SPACE_LG)
        outer.setSpacing(SPACE_MD)

        outer.addWidget(TitleLabel("历史记录", self))
        self._hint = CaptionLabel(
            "每次整理都有完整日志，选中一条即可撤销。默认保留最近 20 次或 30 天内的记录。",
            self,
        )
        self._hint.setWordWrap(True)
        outer.addWidget(self._hint)

        self._tree = QTreeWidget(self)
        self._tree.setColumnCount(6)
        self._tree.setHeaderLabels(
            ["时间", "根目录", "文件数", "策略", "状态", "说明"]
        )
        self._tree.setRootIsDecorated(False)
        self._tree.setUniformRowHeights(True)
        self._tree.setAlternatingRowColors(True)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self._tree.header()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (0, 2, 3, 4, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.currentItemChanged.connect(self._update_actions)
        outer.addWidget(self._tree, stretch=1)

        self._empty = EmptyState(
            "还没有整理记录",
            "完成一次整理后，这里会列出它，并可以随时撤销。",
            parent=self,
        )
        outer.addWidget(self._empty)

        row = QHBoxLayout()
        row.setSpacing(SPACE_SM)
        self._refresh = PushButton("刷新", self)
        self._refresh.clicked.connect(lambda: self.refreshRequested.emit())
        row.addWidget(self._refresh)
        row.addStretch(1)
        self._undo = PrimaryPushButton("撤销所选整理", self)
        self._undo.clicked.connect(self._on_undo)
        self._undo.setEnabled(False)
        row.addWidget(self._undo)
        outer.addLayout(row)

    # -- 填充 -------------------------------------------------------------

    def set_runs(self, summaries: list[RunSummary]) -> None:
        self._tree.clear()
        self._summaries = {s.run_id: s for s in summaries}

        for summary in summaries:
            self._tree.addTopLevelItem(self._make_item(summary))

        has_any = bool(summaries)
        self._tree.setVisible(has_any)
        self._empty.setVisible(not has_any)
        self._update_actions()

    def _make_item(self, summary: RunSummary) -> QTreeWidgetItem:
        meta = summary.meta
        note = ""
        if meta.status is RunStatus.UNFINISHED:
            note = "上次未正常收尾，建议撤销后重新整理"
        elif summary.failed:
            note = f"{summary.failed} 项失败"
        elif summary.removed_dirs:
            note = f"清理了 {summary.removed_dirs} 个空文件夹"

        item = QTreeWidgetItem(
            [
                _pretty_time(meta.started_at),
                meta.root,
                f"{summary.moved:,}",
                _STRATEGY_LABELS.get(meta.strategy, str(meta.strategy)),
                _STATUS_LABELS.get(meta.status, str(meta.status)),
                note,
            ]
        )
        item.setData(0, _RUN_ROLE, meta.run_id)
        item.setToolTip(1, meta.root)
        item.setToolTip(0, f"run id: {meta.run_id}")

        if meta.status is RunStatus.UNFINISHED:
            _tint(item, 4, WARNING)
        elif meta.status is RunStatus.FAILED:
            _tint(item, 4, DANGER)
        return item

    # -- 交互 -------------------------------------------------------------

    def current_run_id(self) -> str | None:
        item = self._tree.currentItem()
        return None if item is None else item.data(0, _RUN_ROLE)

    def _update_actions(self) -> None:
        run_id = self.current_run_id()
        summary = self._summaries.get(run_id) if run_id else None
        # 需求 15.6：已撤销的 run 不可再撤销
        self._undo.setEnabled(summary is not None and summary.meta.is_undoable())

    def _on_undo(self) -> None:
        run_id = self.current_run_id()
        summary = self._summaries.get(run_id) if run_id else None
        if run_id is None or summary is None:
            return
        if not confirm(
            self,
            "撤销这次整理",
            f"时间：{_pretty_time(summary.meta.started_at)}\n"
            f"目录：{summary.meta.root}\n"
            f"影响：{summary.moved} 个文件\n\n"
            "会把这些文件移回原来的位置。整理后被你改动过的文件不会被覆盖。",
            ok_text="撤销",
        ):
            return
        self.undoRequested.emit(run_id)

    def set_busy(self, busy: bool) -> None:
        self._tree.setEnabled(not busy)
        self._refresh.setEnabled(not busy)
        if busy:
            self._undo.setEnabled(False)
        else:
            self._update_actions()


def _tint(item: QTreeWidgetItem, column: int, color: str) -> None:
    from PySide6.QtGui import QColor

    item.setForeground(column, QColor(color))


def _pretty_time(stamp: str) -> str:
    if not stamp:
        return "—"
    try:
        return datetime.fromisoformat(stamp).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return stamp
