"""HistoryManager 的例子级测试。任务 35。

历史记录不是「锦上添花的日志页」——用户可能几天后才发现某次整理有问题，那时唯一
的退路就是这里。因此重点验两件事：**未收尾的 run 永不被保留策略删掉**，以及
**已撤销的 run 不能再撤销**。

Validates: Requirements 13.7, 15.1, 15.2, 15.3, 15.4, 15.5, 15.6
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from app.core.history import UNDO_SUFFIX, HistoryManager
from app.core.models import RunStatus
from tests.fixtures.execution import make_history_run

NOW = datetime(2026, 7, 27, 12, 0, 0).astimezone()


def _stamp(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")


def make_run(history: Path, run_id: str, days_ago: float, **kwargs) -> Path:
    """造一条历史 run。不走真实执行——这里测的是历史管理，不是执行器。"""
    return make_history_run(history, run_id, _stamp(days_ago), **kwargs)


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------


def test_list_runs_is_newest_first(tmp_path: Path) -> None:
    """需求 15.1。"""
    history = tmp_path / "hist"
    make_run(history, "old", days_ago=10)
    make_run(history, "mid", days_ago=5)
    make_run(history, "new", days_ago=1)

    summaries = HistoryManager(history).list_runs()

    assert [s.run_id for s in summaries] == ["new", "mid", "old"]


def test_undo_runs_are_not_listed_as_user_runs(tmp_path: Path) -> None:
    """撤销 run 是实现细节，不是用户做过的一次整理。"""
    history = tmp_path / "hist"
    make_run(history, "r1", days_ago=1)
    make_run(history, f"r1{UNDO_SUFFIX}", days_ago=1)

    summaries = HistoryManager(history).list_runs()

    assert [s.run_id for s in summaries] == ["r1"]


def test_summary_counts_moved_failed_and_removed_dirs(tmp_path: Path) -> None:
    history = tmp_path / "hist"
    make_run(history, "r1", days_ago=1, done=3, failed=2, removed_dirs=1)

    (summary,) = HistoryManager(history).list_runs()

    assert (summary.moved, summary.failed, summary.removed_dirs) == (3, 2, 1)
    assert summary.meta.status is RunStatus.FAILED


def test_missing_history_dir_yields_empty_list(tmp_path: Path) -> None:
    assert HistoryManager(tmp_path / "nope").list_runs() == []


def test_completed_run_has_completed_status(tmp_path: Path) -> None:
    history = tmp_path / "hist"
    make_run(history, "r1", days_ago=1, done=2)

    (summary,) = HistoryManager(history).list_runs()

    assert summary.meta.status is RunStatus.COMPLETED
    assert summary.meta.is_undoable()


def test_unfinished_run_is_detected(tmp_path: Path) -> None:
    """需求 13.7。"""
    history = tmp_path / "hist"
    make_run(history, "r1", days_ago=1, done=1, unfinished=True)

    manager = HistoryManager(history)

    assert [s.run_id for s in manager.find_unfinished()] == ["r1"]
    # 未收尾的 run 仍然可撤销——那正是用户最需要它的时候
    assert manager.is_undoable("r1")


# ---------------------------------------------------------------------------
# 撤销状态
# ---------------------------------------------------------------------------


def test_mark_undone_flips_status_and_blocks_further_undo(tmp_path: Path) -> None:
    """需求 15.6。"""
    history = tmp_path / "hist"
    make_run(history, "r1", days_ago=1)
    manager = HistoryManager(history)

    manager.mark_undone("r1")

    (summary,) = manager.list_runs()
    assert summary.meta.status is RunStatus.UNDONE
    assert summary.meta.undone_at
    assert not summary.meta.is_undoable()
    assert not manager.is_undoable("r1")


def test_clear_undone_restores_undoability(tmp_path: Path) -> None:
    """重做之后该 run 又生效了。"""
    history = tmp_path / "hist"
    make_run(history, "r1", days_ago=1)
    manager = HistoryManager(history)
    manager.mark_undone("r1")

    manager.clear_undone("r1")

    # 标记文件已删，但 `<id>.undo` 目录仍然存在时依旧算已撤销——这里没有它
    assert manager.is_undoable("r1")


def test_presence_of_undo_dir_marks_run_as_undone(tmp_path: Path) -> None:
    history = tmp_path / "hist"
    make_run(history, "r1", days_ago=1)
    (history / f"r1{UNDO_SUFFIX}").mkdir()

    (summary,) = HistoryManager(history).list_runs()

    assert summary.meta.status is RunStatus.UNDONE


def test_mark_undone_on_unknown_run_is_a_no_op(tmp_path: Path) -> None:
    history = tmp_path / "hist"
    history.mkdir()
    HistoryManager(history).mark_undone("nope")
    assert not (history / "nope").exists()


def test_manifest_is_not_rewritten_by_mark_undone(tmp_path: Path) -> None:
    """manifest 是执行前的快照，改写它就失去了「快照」的意义。"""
    history = tmp_path / "hist"
    run_dir = make_run(history, "r1", days_ago=1)
    before = (run_dir / "manifest.json").read_bytes()

    HistoryManager(history).mark_undone("r1")

    assert (run_dir / "manifest.json").read_bytes() == before


# ---------------------------------------------------------------------------
# 保留策略
# ---------------------------------------------------------------------------


def test_retention_drops_the_oldest_beyond_max_runs(tmp_path: Path) -> None:
    """需求 15.3。"""
    history = tmp_path / "hist"
    for index in range(6):
        make_run(history, f"r{index}", days_ago=index)
    manager = HistoryManager(history)

    removed = manager.apply_retention(max_runs=3, max_days=3650, now=NOW)

    assert set(removed) == {"r3", "r4", "r5"}
    assert [s.run_id for s in manager.list_runs()] == ["r0", "r1", "r2"]


def test_retention_drops_runs_older_than_max_days(tmp_path: Path) -> None:
    """需求 15.4。"""
    history = tmp_path / "hist"
    make_run(history, "fresh", days_ago=1)
    make_run(history, "stale", days_ago=45)
    manager = HistoryManager(history)

    removed = manager.apply_retention(max_runs=100, max_days=30, now=NOW)

    assert removed == ["stale"]
    assert [s.run_id for s in manager.list_runs()] == ["fresh"]


def test_retention_never_deletes_unfinished_runs(tmp_path: Path) -> None:
    """未收尾就是用户最需要它的时候，保留策略不该动它。"""
    history = tmp_path / "hist"
    make_run(history, "ancient", days_ago=400, unfinished=True)
    for index in range(5):
        make_run(history, f"r{index}", days_ago=index)
    manager = HistoryManager(history)

    removed = manager.apply_retention(max_runs=1, max_days=1, now=NOW)

    assert "ancient" not in removed
    assert "ancient" in {s.run_id for s in manager.list_runs()}


def test_retention_also_purges_the_paired_undo_run(tmp_path: Path) -> None:
    history = tmp_path / "hist"
    make_run(history, "r0", days_ago=1)
    make_run(history, "stale", days_ago=90)
    (history / f"stale{UNDO_SUFFIX}").mkdir()

    HistoryManager(history).apply_retention(max_runs=10, max_days=30, now=NOW)

    assert not (history / "stale").exists()
    assert not (history / f"stale{UNDO_SUFFIX}").exists()
    assert (history / "r0").is_dir()


def test_retention_is_idempotent(tmp_path: Path) -> None:
    history = tmp_path / "hist"
    for index in range(5):
        make_run(history, f"r{index}", days_ago=index)
    manager = HistoryManager(history)

    first = manager.apply_retention(max_runs=2, max_days=3650, now=NOW)
    second = manager.apply_retention(max_runs=2, max_days=3650, now=NOW)

    assert first and second == []


def test_retention_keeps_everything_when_within_limits(tmp_path: Path) -> None:
    history = tmp_path / "hist"
    for index in range(3):
        make_run(history, f"r{index}", days_ago=index)
    manager = HistoryManager(history)

    assert manager.apply_retention(max_runs=20, max_days=30, now=NOW) == []
    assert len(manager.list_runs()) == 3
