"""执行结果页。需求 16、14.1、14.10。

这一页的重心是**让撤销随手可及**：「撤销本次整理」是页面上最显眼的动作之一，并绑定
Ctrl+Z（需求 14.1）。如果撤销藏在三层菜单里，那它在心理上就等于不存在。

「需人工确认」单独一组呈现（需求 14.6）：这些文件在整理后被改动过，工具拒绝覆盖，
必须让用户看见并自己决定。
"""

from __future__ import annotations

import csv
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.models import (
    CSV_FIELDNAMES,
    ActionKind,
    ExecutionReport,
    Outcome,
    ResultRow,
)
from app.core.progress import ProgressSnapshot
from app.core.undo import UndoReport
from app.ui.theme import (
    DANGER,
    SPACE_LG,
    SPACE_MD,
    SPACE_SM,
    SPACE_XL,
    SUCCESS,
    WARNING,
    BodyLabel,
    CaptionLabel,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    StrongBodyLabel,
    TitleLabel,
    confirm,
)


def _table(headers: list[str], parent: QWidget) -> QTreeWidget:
    tree = QTreeWidget(parent)
    tree.setColumnCount(len(headers))
    tree.setHeaderLabels(headers)
    tree.setRootIsDecorated(False)
    tree.setUniformRowHeights(True)
    tree.setAlternatingRowColors(True)
    tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    header = tree.header()
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    for column in range(1, len(headers)):
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
    return tree


class ResultPage(QWidget):
    """第四步：执行结果与撤销。"""

    undoRequested = Signal()
    redoRequested = Signal()
    openFolderRequested = Signal()
    backRequested = Signal()
    stopRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("resultPage")
        self._report: ExecutionReport | None = None
        self._undone = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACE_XL, SPACE_LG, SPACE_XL, SPACE_LG)
        outer.setSpacing(SPACE_MD)

        self._title = TitleLabel("正在整理…", self)
        outer.addWidget(self._title)

        self._summary = BodyLabel("", self)
        self._summary.setWordWrap(True)
        outer.addWidget(self._summary)

        self._progress = ProgressBar(self)
        self._progress.setRange(0, 100)
        outer.addWidget(self._progress)

        self._detail = CaptionLabel("", self)
        outer.addWidget(self._detail)

        self._tabs = QTabWidget(self)
        self._succeeded = _table(["文件", "目标", "理由"], self)
        self._skipped = _table(["文件", "目标", "原因"], self)
        self._failed = _table(["文件", "目标", "原因"], self)
        self._attention = _table(["原路径", "当前位置", "原因"], self)
        self._tabs.addTab(self._succeeded, "成功")
        self._tabs.addTab(self._skipped, "跳过")
        self._tabs.addTab(self._failed, "失败")
        self._tabs.addTab(self._attention, "需人工确认")
        self._tabs.setTabVisible(3, False)
        outer.addWidget(self._tabs, stretch=1)

        self._cleanup_note = CaptionLabel("", self)
        self._cleanup_note.setWordWrap(True)
        outer.addWidget(self._cleanup_note)

        outer.addLayout(self._build_actions())

        # 需求 14.1：Ctrl+Z 绑定到撤销
        self._undo_shortcut = QShortcut(QKeySequence.StandardKey.Undo, self)
        self._undo_shortcut.activated.connect(self._on_undo)

    def _build_actions(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(SPACE_SM)

        self._back = PushButton("返回方案", self)
        self._back.clicked.connect(lambda: self.backRequested.emit())
        row.addWidget(self._back)

        self._stop = PushButton("停止", self)
        self._stop.clicked.connect(lambda: self.stopRequested.emit())
        row.addWidget(self._stop)

        row.addStretch(1)

        self._export = PushButton("导出 CSV", self)
        self._export.clicked.connect(self._on_export)
        row.addWidget(self._export)

        self._open = PushButton("打开目录", self)
        self._open.clicked.connect(lambda: self.openFolderRequested.emit())
        row.addWidget(self._open)

        self._redo = PushButton("重做", self)
        self._redo.clicked.connect(lambda: self.redoRequested.emit())
        self._redo.setVisible(False)
        row.addWidget(self._redo)

        self._undo = PrimaryPushButton("撤销本次整理", self)
        self._undo.clicked.connect(self._on_undo)
        row.addWidget(self._undo)
        return row

    # -- 状态 -------------------------------------------------------------

    def show_running(self, total: int, dry_run: bool = False) -> None:
        self._title.setText("正在模拟运行…" if dry_run else "正在整理…")
        self._summary.setText(
            "模拟运行不会改动任何文件。" if dry_run else "正在移动文件，可以随时停止。"
        )
        self._progress.setRange(0, max(1, total))
        self._progress.setValue(0)
        self._detail.setText("")
        for table in (self._succeeded, self._skipped, self._failed, self._attention):
            table.clear()
        self._set_running(True)

    def show_progress(self, snapshot: ProgressSnapshot) -> None:
        if snapshot.total:
            self._progress.setRange(0, snapshot.total)
        self._progress.setValue(snapshot.processed)
        text = f"已处理 {snapshot.processed:,}"
        if snapshot.total:
            text += f" / {snapshot.total:,}"
        if snapshot.current:
            text += f" · {snapshot.current}"
        self._detail.setText(text)

    def show_report(self, report: ExecutionReport, undoable: bool) -> None:
        """需求 16.2：按成功 / 跳过 / 失败三组展示并显示计数。"""
        self._report = report
        self._undone = False
        counts = report.counts()

        if report.dry_run:
            self._title.setText("模拟运行完成")
        elif report.stopped:
            self._title.setText("已停止")
        else:
            self._title.setText("整理完成")
        # 需求 11.7：停止后的撤销就是「回滚已完成部分」——同一个动作，换个说法能让
        # 用户看懂它此刻的含义
        self._undo.setText(
            "回滚已完成部分" if report.stopped else "撤销本次整理"
        )
        self._summary.setText(self._summary_text(report))
        self._progress.setRange(0, max(1, report.total()))
        self._progress.setValue(report.total())
        self._detail.setText(f"耗时 {report.elapsed_ms / 1000:.1f} 秒")

        self._fill(self._succeeded, report.succeeded)
        self._fill(self._skipped, report.skipped)
        self._fill(self._failed, report.failed)

        self._tabs.setTabText(0, f"成功 {counts[Outcome.SUCCEEDED]}")
        self._tabs.setTabText(1, f"跳过 {counts[Outcome.SKIPPED]}")
        self._tabs.setTabText(2, f"失败 {counts[Outcome.FAILED]}")
        self._tabs.setTabVisible(3, False)

        self._cleanup_note.setText(self._cleanup_text(report))
        self._set_running(False)
        self._undo.setVisible(not report.dry_run)
        self._undo.setEnabled(undoable and not report.dry_run)
        self._redo.setVisible(False)

    def show_undo_report(self, report: UndoReport) -> None:
        self._undone = True
        self._title.setText("已撤销")
        self._summary.setText(report.summary())
        self._detail.setText("")

        rows = [
            ResultRow(
                src=item.src,
                dst=item.dst,
                # 这几行只是给「需人工确认」表格填数，action 维度对它们没有意义；
                # 撤销的动作恒为移动，写 MOVE 比塞一个 Outcome 进 action 字段诚实
                action=ActionKind.MOVE,
                outcome=Outcome.SKIPPED,
                reason=item.reason,
            )
            for item in [*report.needs_attention, *report.failed]
        ]
        self._fill(self._attention, rows)
        has_attention = bool(rows)
        self._tabs.setTabVisible(3, has_attention)
        self._tabs.setTabText(3, f"需人工确认 {len(rows)}")
        if has_attention:
            self._tabs.setCurrentIndex(3)

        self._undo.setEnabled(False)
        self._redo.setVisible(True)

    def set_busy(self, busy: bool) -> None:
        self._set_running(busy)

    def _set_running(self, running: bool) -> None:
        self._stop.setEnabled(running)
        self._progress.setVisible(True)
        for widget in (self._back, self._export, self._open, self._undo, self._redo):
            widget.setEnabled(not running)

    # -- 内部 -------------------------------------------------------------

    @staticmethod
    def _summary_text(report: ExecutionReport) -> str:
        counts = report.counts()
        if report.dry_run:
            return (
                f"这次只是模拟：{counts[Outcome.SUCCEEDED]} 个文件会被移动，"
                f"{counts[Outcome.SKIPPED]} 个会跳过，{counts[Outcome.FAILED]} 个会失败。"
                "磁盘上什么都没变。"
            )
        if report.stopped:
            # 需求 11.7：停止之后要能把已完成的部分退回去
            return (
                f"已停止。停止前完成了 {counts[Outcome.SUCCEEDED]} 个文件，"
                f"剩下 {counts[Outcome.SKIPPED]} 个没有动。"
                "点右下角可以把已完成的部分整体退回原位。"
            )
        parts = [f"成功 {counts[Outcome.SUCCEEDED]}"]
        if counts[Outcome.SKIPPED]:
            parts.append(f"跳过 {counts[Outcome.SKIPPED]}")
        if counts[Outcome.FAILED]:
            parts.append(f"失败 {counts[Outcome.FAILED]}")
        return "，".join(parts) + "。如果结果不对，点右下角撤销即可完整还原。"

    @staticmethod
    def _cleanup_text(report: ExecutionReport) -> str:
        if report.dry_run and report.predicted_removed_dirs:
            listed = "、".join(p.name for p in report.predicted_removed_dirs[:5])
            more = (
                f" 等 {len(report.predicted_removed_dirs)} 个"
                if len(report.predicted_removed_dirs) > 5
                else ""
            )
            return f"将清理这些变空的子文件夹：{listed}{more}"
        if report.removed_dirs:
            return f"已清理 {len(report.removed_dirs)} 个变空的子文件夹（撤销时会重建）。"
        return ""

    @staticmethod
    def _fill(table: QTreeWidget, rows: list[ResultRow]) -> None:
        table.clear()
        colors = {
            Outcome.SUCCEEDED: SUCCESS,
            Outcome.SKIPPED: WARNING,
            Outcome.FAILED: DANGER,
        }
        for row in rows:
            item = QTreeWidgetItem(
                [Path(row.src).name, str(row.dst), row.reason]
            )
            item.setToolTip(0, row.src)
            item.setToolTip(1, row.dst)
            color = colors.get(row.outcome)
            if color and row.outcome is not Outcome.SUCCEEDED:
                from PySide6.QtGui import QColor

                item.setForeground(2, QColor(color))
            table.addTopLevelItem(item)

    def _on_undo(self) -> None:
        if not self._undo.isEnabled() or self._undone:
            return
        report = self._report
        moved = len(report.succeeded) if report else 0
        if not confirm(
            self,
            "撤销本次整理",
            f"会把 {moved} 个文件移回原来的位置。\n\n"
            "整理后被你改动过的文件不会被覆盖，它们会列在「需人工确认」里。",
            ok_text="撤销",
        ):
            return
        self.undoRequested.emit()

    def _on_export(self) -> None:
        """需求 16.4：导出 CSV。"""
        report = self._report
        if report is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出结果", "整理结果.csv", "CSV 文件 (*.csv)"
        )
        if not path:
            return
        rows = report.to_csv_rows()
        # utf-8-sig：Excel 打开不带 BOM 的 UTF-8 CSV 会把中文显示成乱码
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(CSV_FIELDNAMES))
            writer.writeheader()
            writer.writerows(rows)
