"""执行结果页、历史记录页与执行/撤销服务的冒烟测试。任务 36、37、38。

M4 的 core 逻辑有 100 多条测试，但「用户点下去到底会发生什么」还差一段：
``ExecuteService`` / ``UndoService`` 把 core 放进 ``QThread``，那层线程与信号翻译
出错的话，core 再对也没用。最后一个测试就是替代手工点一遍。

Validates: Requirements 11.6, 11.7, 12.7, 14.1, 14.10, 15.1, 15.6, 16.1, 16.2,
16.3, 16.4, 16.5
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from app.core.history import HistoryManager, RunSummary
from app.core.models import (
    ActionKind,
    ExecOptions,
    ExecutionReport,
    Outcome,
    ResultRow,
    RunMeta,
    RunStatus,
    Strategy,
)
from app.core.progress import ProgressSnapshot
from app.core.undo import AttentionItem, UndoReport
from tests.fixtures.execution import build_plan, make_history_run, tree_paths
from tests.fixtures.trees import build_tree

pytest.importorskip("pytestqt", reason="需要 pytest-qt 才能驱动 Qt 事件循环")


def _row(name: str, outcome: Outcome) -> ResultRow:
    return ResultRow(
        src=f"C:\\root\\{name}",
        dst=f"C:\\root\\文档\\{name}",
        action=ActionKind.MOVE,
        outcome=outcome,
        reason="扩展名规则",
    )


def _report() -> ExecutionReport:
    return ExecutionReport(
        succeeded=[_row("a.pdf", Outcome.SUCCEEDED), _row("b.pdf", Outcome.SUCCEEDED)],
        skipped=[_row("c.pdf", Outcome.SKIPPED)],
        failed=[_row("d.pdf", Outcome.FAILED)],
        elapsed_ms=1234,
    )


# ---------------------------------------------------------------------------
# 结果页
# ---------------------------------------------------------------------------


def test_result_page_shows_three_groups_with_counts(qtbot) -> None:
    """需求 16.2。"""
    from app.ui.pages.result_page import ResultPage

    page = ResultPage()
    qtbot.addWidget(page)

    page.show_report(_report(), undoable=True)

    tabs = page._tabs  # noqa: SLF001
    assert tabs.tabText(0) == "成功 2"
    assert tabs.tabText(1) == "跳过 1"
    assert tabs.tabText(2) == "失败 1"
    assert page._succeeded.topLevelItemCount() == 2  # noqa: SLF001
    assert page._skipped.topLevelItemCount() == 1  # noqa: SLF001
    assert page._failed.topLevelItemCount() == 1  # noqa: SLF001


def test_result_page_undo_button_is_enabled_only_when_undoable(qtbot) -> None:
    """需求 14.1：撤销必须随手可及；不可撤销时明确禁用。"""
    from app.ui.pages.result_page import ResultPage

    page = ResultPage()
    qtbot.addWidget(page)

    page.show_report(_report(), undoable=True)
    assert page._undo.isEnabled()  # noqa: SLF001

    page.show_report(_report(), undoable=False)
    assert not page._undo.isEnabled()  # noqa: SLF001


def test_dry_run_report_hides_undo(qtbot) -> None:
    """模拟运行没有副作用，也就没有可撤销的东西。"""
    from app.ui.pages.result_page import ResultPage

    page = ResultPage()
    qtbot.addWidget(page)

    report = _report()
    report.dry_run = True
    page.show_report(report, undoable=True)

    # 用 isHidden 而不是 isVisible：页面本身没 show()，离屏平台下所有子控件的
    # isVisible() 都是 False，那样的断言恒真、测不到任何东西
    assert page._undo.isHidden()  # noqa: SLF001
    assert "模拟" in page._title.text()  # noqa: SLF001


def test_result_page_binds_ctrl_z_to_undo(qtbot) -> None:
    """需求 14.1。"""
    from PySide6.QtGui import QKeySequence

    from app.ui.pages.result_page import ResultPage

    page = ResultPage()
    qtbot.addWidget(page)

    assert page._undo_shortcut.key() == QKeySequence(  # noqa: SLF001
        QKeySequence.StandardKey.Undo
    )


def test_result_page_progress_updates(qtbot) -> None:
    """需求 16.1。"""
    from app.ui.pages.result_page import ResultPage

    page = ResultPage()
    qtbot.addWidget(page)

    page.show_running(total=10)
    page.show_progress(ProgressSnapshot("execute", 4, 10, current="a.pdf"))

    assert page._progress.value() == 4  # noqa: SLF001
    assert "4" in page._detail.text()  # noqa: SLF001


def test_undo_report_surfaces_attention_items(qtbot) -> None:
    """需求 14.6：被外部修改而未还原的文件必须让用户看见。"""
    from app.ui.pages.result_page import ResultPage

    page = ResultPage()
    qtbot.addWidget(page)
    page.show_report(_report(), undoable=True)

    page.show_undo_report(
        UndoReport(
            restored=1,
            needs_attention=[
                AttentionItem(
                    src="C:\\root\\a.pdf",
                    dst="C:\\root\\文档\\a.pdf",
                    reason="文件在整理之后被修改过",
                    recorded_size=10,
                    current_size=20,
                )
            ],
        )
    )

    assert page._tabs.isTabVisible(3)  # noqa: SLF001
    assert page._tabs.tabText(3) == "需人工确认 1"  # noqa: SLF001
    assert page._attention.topLevelItemCount() == 1  # noqa: SLF001
    # 撤销完成后重做出现，撤销自身禁用
    assert not page._redo.isHidden()  # noqa: SLF001
    assert not page._undo.isEnabled()  # noqa: SLF001


def test_csv_export_row_count_equals_group_totals(tmp_path: Path) -> None:
    """需求 16.5。行数等于三组计数之和，由 to_csv_rows 保证。"""
    report = _report()
    path = tmp_path / "结果.csv"

    from app.core.models import CSV_FIELDNAMES

    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_FIELDNAMES))
        writer.writeheader()
        writer.writerows(report.to_csv_rows())

    with open(path, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == report.total() == 4
    assert list(rows[0].keys()) == list(CSV_FIELDNAMES)


# ---------------------------------------------------------------------------
# 历史记录页
# ---------------------------------------------------------------------------


def _summary(run_id: str, status: RunStatus, moved: int = 3) -> RunSummary:
    return RunSummary(
        meta=RunMeta(
            run_id=run_id,
            started_at="2026-07-27T10:00:00+08:00",
            root="C:\\下载",
            strategy=Strategy.BY_TYPE,
            file_count=moved,
            status=status,
        ),
        moved=moved,
    )


def test_history_page_lists_runs(qtbot) -> None:
    """需求 15.1。"""
    from app.ui.pages.history_page import HistoryPage

    page = HistoryPage()
    qtbot.addWidget(page)

    page.set_runs(
        [
            _summary("r2", RunStatus.COMPLETED),
            _summary("r1", RunStatus.UNDONE),
        ]
    )

    assert page._tree.topLevelItemCount() == 2  # noqa: SLF001
    assert page._tree.topLevelItem(0).text(4) == "已完成"  # noqa: SLF001
    assert page._tree.topLevelItem(1).text(4) == "已撤销"  # noqa: SLF001


def test_history_page_shows_empty_state(qtbot) -> None:
    from app.ui.pages.history_page import HistoryPage

    page = HistoryPage()
    qtbot.addWidget(page)

    page.set_runs([])

    assert not page._tree.isVisible()  # noqa: SLF001
    assert not page._undo.isEnabled()  # noqa: SLF001


def test_history_page_disables_undo_for_undone_run(qtbot) -> None:
    """需求 15.6。"""
    from app.ui.pages.history_page import HistoryPage

    page = HistoryPage()
    qtbot.addWidget(page)
    page.set_runs([_summary("r1", RunStatus.UNDONE)])

    page._tree.setCurrentItem(page._tree.topLevelItem(0))  # noqa: SLF001

    assert page.current_run_id() == "r1"
    assert not page._undo.isEnabled()  # noqa: SLF001


def test_history_page_enables_undo_for_unfinished_run(qtbot) -> None:
    """未收尾的 run 正是最需要撤销的时候。"""
    from app.ui.pages.history_page import HistoryPage

    page = HistoryPage()
    qtbot.addWidget(page)
    page.set_runs([_summary("r1", RunStatus.UNFINISHED)])

    page._tree.setCurrentItem(page._tree.topLevelItem(0))  # noqa: SLF001

    assert page._undo.isEnabled()  # noqa: SLF001
    assert page._tree.topLevelItem(0).text(4) == "未收尾"  # noqa: SLF001


def test_history_page_reads_real_history(qtbot, tmp_path: Path) -> None:
    """把 HistoryManager 的真实输出喂给页面，验证字段对得上。"""
    from app.ui.pages.history_page import HistoryPage

    history = tmp_path / "hist"
    make_history_run(history, "run-01", "2026-07-27T10:00:00+08:00", done=4)

    page = HistoryPage()
    qtbot.addWidget(page)
    page.set_runs(HistoryManager(history).list_runs())

    assert page._tree.topLevelItemCount() == 1  # noqa: SLF001
    assert page._tree.topLevelItem(0).text(2) == "4"  # noqa: SLF001


# ---------------------------------------------------------------------------
# 服务层：执行 → 撤销走一遍真实线程
# ---------------------------------------------------------------------------


def test_execute_then_undo_through_services(qtbot, tmp_path: Path) -> None:
    """替代手工点一遍：ExecuteService 执行、UndoService 撤销，文件真回原位。"""
    from app.services.execute_service import ExecuteService, UndoService

    root = build_tree(
        tmp_path / "root", {"报告.pdf": "内容", "图.png": "像素"}
    ).resolve()
    history = tmp_path / "hist"
    before = tree_paths(root)

    plan, selection = build_plan(root)
    execute = ExecuteService(history)
    undo = UndoService(history)

    run_ids: list[str] = []
    execute.runStarted.connect(run_ids.append)

    with qtbot.waitSignal(execute.finished, timeout=20000) as blocker:
        assert execute.start_execute(plan, ExecOptions(), selection)
    report = blocker.args[0]

    assert report.total() == 2
    assert not report.failed, [r.reason for r in report.failed]
    assert (root / "文档" / "PDF" / "报告.pdf").is_file()
    assert (root / "图片" / "图.png").is_file()
    assert run_ids and execute.last_run_id == run_ids[0]

    summaries = HistoryManager(history).list_runs()
    assert [s.run_id for s in summaries] == [run_ids[0]]
    assert summaries[0].meta.status is RunStatus.COMPLETED

    with qtbot.waitSignal(undo.finished, timeout=20000) as blocker:
        assert undo.start_undo(run_ids[0])
    undo_report = blocker.args[0]

    assert undo_report.ok, undo_report.summary()
    assert undo_report.restored == 2
    assert tree_paths(root) == before

    execute.wait(5000)
    undo.wait(5000)


def test_dry_run_through_service_writes_no_history(qtbot, tmp_path: Path) -> None:
    """需求 11.8：模拟运行不产生可撤销的副作用，也就不该出现在历史里。"""
    from app.services.execute_service import ExecuteService

    root = build_tree(tmp_path / "root", {"a.pdf": "x"}).resolve()
    history = tmp_path / "hist"
    before = tree_paths(root)

    plan, selection = build_plan(root)
    service = ExecuteService(history)

    with qtbot.waitSignal(service.finished, timeout=20000) as blocker:
        assert service.start_execute(plan, ExecOptions(dry_run=True), selection)

    assert blocker.args[0].dry_run is True
    assert tree_paths(root) == before
    assert service.last_run_id is None
    assert not history.exists()

    service.wait(5000)


def test_execute_service_emits_progress(qtbot, tmp_path: Path) -> None:
    """需求 12.7：进度经节流回调，UI 才有东西可显示。"""
    from app.services.execute_service import ExecuteService

    root = build_tree(
        tmp_path / "root", {f"{i}.pdf": "x" * 100 for i in range(8)}
    ).resolve()
    plan, selection = build_plan(root)
    service = ExecuteService(tmp_path / "hist")

    seen: list[ProgressSnapshot] = []
    service.progressChanged.connect(seen.append)

    with qtbot.waitSignal(service.finished, timeout=20000):
        service.start_execute(plan, ExecOptions(), selection)

    assert seen, "至少要发出一条进度"
    assert seen[-1].final is True
    service.wait(5000)


# ---------------------------------------------------------------------------
# 需求 11.4：确认后的 3 秒取消窗口
# ---------------------------------------------------------------------------


def test_countdown_dialog_accepts_after_the_window_elapses(qtbot) -> None:
    """倒计时走完即视为继续执行。"""
    from PySide6.QtWidgets import QDialog, QWidget

    from app.ui.theme import CountdownDialog

    host = QWidget()
    qtbot.addWidget(host)
    dialog = CountdownDialog(host, "3 个文件", seconds=2, interval_ms=10)
    qtbot.addWidget(dialog)

    with qtbot.waitSignal(dialog.accepted, timeout=3000):
        dialog.open()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.remaining <= 0


def test_countdown_dialog_can_be_cancelled(qtbot) -> None:
    """需求 11.4：窗口内取消则放弃执行。"""
    from PySide6.QtWidgets import QDialog, QWidget

    from app.ui.theme import CountdownDialog

    host = QWidget()
    qtbot.addWidget(host)
    dialog = CountdownDialog(host, "3 个文件", seconds=30, interval_ms=1000)
    qtbot.addWidget(dialog)
    dialog.open()

    dialog._cancel.click()  # noqa: SLF001

    assert dialog.result() == QDialog.DialogCode.Rejected
    assert dialog.remaining == 30, "取消不该消耗倒计时"


def test_countdown_cancel_button_is_large_and_default(qtbot) -> None:
    """「大号取消按钮」是需求写明的，不是口味问题。"""
    from PySide6.QtWidgets import QWidget

    from app.ui.theme import CountdownDialog

    host = QWidget()
    qtbot.addWidget(host)
    dialog = CountdownDialog(host, "3 个文件", seconds=3, interval_ms=1000)
    qtbot.addWidget(dialog)

    assert dialog._cancel.minimumHeight() >= 40  # noqa: SLF001
    assert dialog._cancel.isDefault()  # noqa: SLF001
    assert "取消" in dialog._cancel.text()  # noqa: SLF001


def test_stopped_report_offers_rollback(qtbot) -> None:
    """需求 11.7：停止后提供「回滚已完成部分」。"""
    from app.ui.pages.result_page import ResultPage

    page = ResultPage()
    qtbot.addWidget(page)

    report = _report()
    report.stopped = True
    page.show_report(report, undoable=True)

    assert page._title.text() == "已停止"  # noqa: SLF001
    assert page._undo.text() == "回滚已完成部分"  # noqa: SLF001
    assert page._undo.isEnabled()  # noqa: SLF001


def test_normal_report_keeps_the_undo_label(qtbot) -> None:
    from app.ui.pages.result_page import ResultPage

    page = ResultPage()
    qtbot.addWidget(page)
    page.show_report(_report(), undoable=True)

    assert page._undo.text() == "撤销本次整理"  # noqa: SLF001


# ---------------------------------------------------------------------------
# 需求 13.7、13.8、14.11：崩溃恢复的三个出口
# ---------------------------------------------------------------------------


def test_choice_dialog_reports_the_picked_index(qtbot) -> None:
    from PySide6.QtWidgets import QWidget

    from app.ui.theme import ChoiceDialog

    host = QWidget()
    qtbot.addWidget(host)
    dialog = ChoiceDialog(host, "标题", "说明", ["恢复执行", "全部撤销", "忽略"])
    qtbot.addWidget(dialog)
    dialog.open()

    dialog.buttons[0].click()

    assert dialog.chosen == 0


def test_choice_dialog_defaults_to_the_most_conservative_option(qtbot) -> None:
    """回车不该触发破坏性选项，所以默认落在最后一个。"""
    from PySide6.QtWidgets import QWidget

    from app.ui.theme import ChoiceDialog

    host = QWidget()
    qtbot.addWidget(host)
    dialog = ChoiceDialog(host, "标题", "说明", ["恢复执行", "全部撤销", "忽略"])
    qtbot.addWidget(dialog)

    assert dialog.buttons[-1].isDefault()
    assert dialog.chosen is None


def test_resume_finishes_the_interrupted_run(qtbot, tmp_path: Path) -> None:
    """需求 13.8：恢复执行把仍在原处的文件接着搬完，并写进同一份 journal。"""
    from app.core.journal import JOURNAL_NAME, Journal
    from app.core.models import RecordKind
    from app.core.undo import UndoManager
    from app.services.execute_service import ExecuteService
    from tests.fixtures.doubles import TrashRecorder

    root = build_tree(
        tmp_path / "root", {"a.pdf": "1", "b.pdf": "2", "c.png": "3"}
    ).resolve()
    history = tmp_path / "hist"
    before = tree_paths(root)

    plan, selection = build_plan(root)
    service = ExecuteService(history)
    run_ids: list[str] = []
    service.runStarted.connect(run_ids.append)

    with qtbot.waitSignal(service.finished, timeout=20000):
        service.start_execute(plan, ExecOptions(), selection)
    run_id = run_ids[0]
    after_full = tree_paths(root)

    # 把最后一条 done 连同它移动的文件一起「回退」，伪造一次中断：
    # 日志里只剩 intent，文件还在原处
    run_dir = history / run_id
    records = Journal.read_records(run_dir)
    victim = [r for r in records if r.kind is RecordKind.DONE][-1]
    Path(victim.dst).replace(Path(victim.src))
    lines = [
        line
        for line in (run_dir / JOURNAL_NAME).read_text("utf-8").split("\n")
        if line.strip() and f'"seq": {victim.seq},' not in line
    ]
    (run_dir / JOURNAL_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")

    manager = UndoManager(history, trash=TrashRecorder())
    pending = manager.resume(run_id)
    assert pending.at_source == [victim.src]

    with qtbot.waitSignal(service.finished, timeout=20000) as blocker:
        assert service.start_resume(run_id, pending.at_source, selection)
    report = blocker.args[0]

    assert report.total() == 1
    assert not report.failed, [r.reason for r in report.failed]
    assert tree_paths(root) == after_full, "恢复执行后应当与完整执行的结果一致"

    # seq 接着写，没有重复；撤销仍能整体回滚
    resumed = Journal.read_records(run_dir)
    assert len(resumed) == len({r.seq for r in resumed})
    assert not manager.resume(run_id).at_source

    undo = manager.undo(run_id)
    assert undo.ok, undo.summary()
    assert tree_paths(root) == before

    service.wait(5000)


def test_undo_flags_intent_only_entries_as_needing_attention(
    tmp_path: Path,
) -> None:
    """需求 14.11：停在 intent 的条目必须被列出来，不能悄悄放过。"""
    from app.core.journal import Journal
    from app.core.models import RecordKind
    from app.core.undo import UndoManager
    from tests.fixtures.doubles import TrashRecorder

    root = build_tree(tmp_path / "root", {"a.pdf": "x"}).resolve()
    run_dir = tmp_path / "hist" / "run-crash"
    with Journal(run_dir) as journal:
        journal.append(
            RecordKind.INTENT,
            src=root / "a.pdf",
            dst=root / "文档" / "PDF" / "a.pdf",
            size=1,
        )

    report = UndoManager(tmp_path / "hist", trash=TrashRecorder()).undo("run-crash")

    assert report.restored == 0
    assert len(report.needs_attention) == 1
    assert report.needs_attention[0].src == str(root / "a.pdf")
    assert "仍在原处" in report.needs_attention[0].reason
    assert (root / "a.pdf").is_file()
