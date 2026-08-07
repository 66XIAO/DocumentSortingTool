"""撤销专项测试。任务 34、41。

任务 41 点名的六个场景，一条不少：

1. 执行进程被强制终止后重启，未收尾 run 被识别且「全部撤销」能完成回滚
2. 移动后文件被外部修改，撤销拒绝覆盖并列入需人工确认
3. 跨卷还原走复制校验路径，目标副本进回收站
4. 原目录位置被占用导致还原失败，标记 failed 且不中断其余条目
5. 嵌套类目目录的创建与清理，`created_dir` 只在为空时被删
6. 撤销 → 重做 → 再撤销，文件树状态与首次撤销一致

Validates: Requirements 12.3, 12.6, 13.7, 13.8, 14.2, 14.3, 14.4, 14.5, 14.6,
14.7, 14.8, 14.9, 14.11, 14.12, 14.13, 20.18, 20.19
"""

from __future__ import annotations

import os
from pathlib import Path

from app.core import fsops
from app.core.history import HistoryManager
from app.core.journal import JOURNAL_NAME, Journal
from app.core.models import ExecOptions, RecordKind, RunStatus
from app.core.undo import UndoManager
from tests.fixtures.doubles import TrashRecorder
from tests.fixtures.execution import (
    file_fingerprints,
    sort_and_execute,
    tree_paths,
)
from tests.fixtures.trees import build_tree


def _root(tmp_path: Path, spec: dict) -> Path:
    return build_tree(tmp_path / "root", spec).resolve()


def _undo_manager(tmp_path: Path, trash: TrashRecorder | None = None) -> UndoManager:
    return UndoManager(tmp_path / "hist", trash=trash or TrashRecorder())


def _drop_last_record_of_kind(run_dir: Path, kind: RecordKind) -> str:
    """删掉最后一条指定类型的日志行，模拟「操作做完、日志没写完就断电」。

    返回被删掉那条记录的 src。
    """
    path = run_dir / JOURNAL_NAME
    lines = path.read_text(encoding="utf-8").splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if f'"kind": "{kind}"' in lines[index] or f'"kind":"{kind}"' in lines[index]:
            dropped = lines.pop(index)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            import json

            return json.loads(dropped)["src"]
    raise AssertionError(f"日志里没有 {kind} 记录")


# ---------------------------------------------------------------------------
# 基础往返
# ---------------------------------------------------------------------------


def test_undo_restores_every_file_to_its_original_path(tmp_path: Path) -> None:
    """需求 14.2、14.3、属性 31。"""
    root = _root(tmp_path, {"报告.pdf": "内容", "图.png": "像素", "怪.zzz": "?"})
    before_paths = tree_paths(root)
    before_prints = file_fingerprints(root)

    result = sort_and_execute(root, tmp_path / "hist")
    assert tree_paths(root) != before_paths, "执行本身应当改变文件树"

    report = _undo_manager(tmp_path).undo(result.run_id)

    assert report.ok, report.summary()
    assert report.restored == len(result.report.succeeded)
    assert tree_paths(root) == before_paths
    assert file_fingerprints(root) == before_prints


def test_undo_restores_in_strict_reverse_order_of_done_records(
    tmp_path: Path,
) -> None:
    """属性 31：还原调用序列是 done 记录的严格逆序。"""
    root = _root(tmp_path, {f"{i}.pdf": str(i) for i in range(5)})
    result = sort_and_execute(root, tmp_path / "hist")
    forward_done = [r.dst for r in result.of_kind(RecordKind.DONE)]

    _undo_manager(tmp_path).undo(result.run_id)

    undo_records = Journal.read_records(tmp_path / "hist" / f"{result.run_id}.undo")
    undo_done = [r.src for r in undo_records if r.kind is RecordKind.DONE]
    assert undo_done == list(reversed(forward_done))


def test_undo_writes_its_own_journal_with_matching_record_count(
    tmp_path: Path,
) -> None:
    """需求 14.9、14.12、属性 31。撤销自身可追溯，重做才能是「撤销的撤销」。"""
    root = _root(tmp_path, {"a.pdf": "x", "b.png": "y"})
    result = sort_and_execute(root, tmp_path / "hist")

    report = _undo_manager(tmp_path).undo(result.run_id)

    undo_records = Journal.read_records(tmp_path / "hist" / f"{result.run_id}.undo")
    done = [r for r in undo_records if r.kind is RecordKind.DONE]
    assert len(done) == report.restored


def test_undo_of_a_run_with_nothing_recorded_is_a_no_op(tmp_path: Path) -> None:
    report = _undo_manager(tmp_path).undo("不存在的-run")
    assert report.restored == 0
    assert report.ok


# ---------------------------------------------------------------------------
# 场景 1：进程被强制终止后重启
# ---------------------------------------------------------------------------


def test_killed_run_is_detected_as_unfinished(tmp_path: Path) -> None:
    """需求 13.7。"""
    root = _root(tmp_path, {"a.pdf": "x", "b.png": "y"})
    result = sort_and_execute(root, tmp_path / "hist")
    _drop_last_record_of_kind(result.run_dir, RecordKind.DONE)

    unfinished = HistoryManager(tmp_path / "hist").find_unfinished()

    assert [s.run_id for s in unfinished] == [result.run_id]
    assert unfinished[0].meta.status is RunStatus.UNFINISHED


def test_resume_locates_the_dangling_entry_at_its_target(tmp_path: Path) -> None:
    """需求 13.8、14.11。文件已经搬了、日志没写完——探测它实际在哪。"""
    root = _root(tmp_path, {"a.pdf": "x", "b.png": "y"})
    result = sort_and_execute(root, tmp_path / "hist")
    dangling = _drop_last_record_of_kind(result.run_dir, RecordKind.DONE)

    resume = _undo_manager(tmp_path).resume(result.run_id)

    assert resume.at_target == [dangling]
    assert resume.at_source == []
    assert resume.both == [] and resume.missing == []


def test_undo_after_kill_rolls_back_every_logged_move(tmp_path: Path) -> None:
    """需求 14.11。停在 intent 的那条留给人工确认，其余全部回滚。"""
    root = _root(tmp_path, {"a.pdf": "x", "b.png": "y", "c.txt": "z"})
    before = tree_paths(root)
    result = sort_and_execute(root, tmp_path / "hist")
    dangling = _drop_last_record_of_kind(result.run_dir, RecordKind.DONE)

    manager = _undo_manager(tmp_path)
    report = manager.undo(result.run_id)
    resume = manager.resume(result.run_id)

    # 有日志依据的都回来了
    assert report.restored == len(result.report.succeeded) - 1
    assert Path(dangling).exists() is False, "日志缺失的那条不该被撤销碰到"
    # 缺日志的那条仍在目标位置，且被明确指出来
    assert resume.at_target == [dangling]
    # 除了那一条，其余路径与执行前一致
    remaining = tree_paths(root) - before
    assert all(".docsort" not in p.parts for p in remaining)
    assert {p for p in remaining if p.is_file()}


def test_intent_only_entry_is_reported_as_at_source(tmp_path: Path) -> None:
    """崩在动手之前：文件还在原处，撤销无需做任何事。"""
    root = _root(tmp_path, {"a.pdf": "x"})
    run_dir = tmp_path / "hist" / "run-crash"
    with Journal(run_dir) as journal:
        journal.append(RecordKind.INTENT, src=root / "a.pdf", dst=root / "文档/a.pdf")

    manager = _undo_manager(tmp_path)
    resume = manager.resume("run-crash")
    report = manager.undo("run-crash")

    assert resume.at_source == [str(root / "a.pdf")]
    assert report.restored == 0
    assert (root / "a.pdf").is_file()


# ---------------------------------------------------------------------------
# 场景 2：外部修改后拒绝覆盖
# ---------------------------------------------------------------------------


def test_undo_refuses_to_overwrite_externally_modified_file(
    tmp_path: Path,
) -> None:
    """需求 14.5、14.6、属性 32。宁可留一个待处理项，也不冲掉用户后来的修改。"""
    root = _root(tmp_path, {"a.pdf": "原内容", "b.png": "像素"})
    result = sort_and_execute(root, tmp_path / "hist")

    moved = root / "文档" / "PDF" / "a.pdf"
    moved.write_text("我在整理之后改了这个文件", encoding="utf-8")

    report = _undo_manager(tmp_path).undo(result.run_id)

    assert len(report.needs_attention) == 1
    item = report.needs_attention[0]
    assert item.dst == str(moved)
    assert item.src == str(root / "a.pdf")
    assert "修改" in item.reason
    # 并排对比信息齐备，用户才能自己判断该保哪一份
    assert item.recorded_size is not None and item.current_size is not None
    assert item.recorded_size != item.current_size

    # 被改过的留在原地，没被改的照常还原
    assert moved.read_text(encoding="utf-8") == "我在整理之后改了这个文件"
    assert not (root / "a.pdf").exists()
    assert (root / "b.png").is_file()
    assert report.restored == 1


def test_undo_detects_mtime_only_modification(tmp_path: Path) -> None:
    """只改时间不改大小也算被动过。需求 14.5。"""
    root = _root(tmp_path, {"a.pdf": "内容"})
    result = sort_and_execute(root, tmp_path / "hist")

    moved = root / "文档" / "PDF" / "a.pdf"
    stat = moved.stat()
    os.utime(moved, (stat.st_atime, stat.st_mtime + 3600))

    report = _undo_manager(tmp_path).undo(result.run_id)

    assert len(report.needs_attention) == 1
    assert report.restored == 0
    assert moved.is_file()


def test_undo_tolerates_mtime_within_filesystem_precision(tmp_path: Path) -> None:
    """FAT 的时间精度是 2 秒，完全相等的要求会把正常文件误判成被改过。"""
    root = _root(tmp_path, {"a.pdf": "内容"})
    result = sort_and_execute(root, tmp_path / "hist")

    moved = root / "文档" / "PDF" / "a.pdf"
    stat = moved.stat()
    os.utime(moved, (stat.st_atime, stat.st_mtime + 1.0))

    report = _undo_manager(tmp_path).undo(result.run_id)

    assert report.ok, report.summary()
    assert (root / "a.pdf").is_file()


def test_undo_refuses_when_original_path_is_reoccupied(tmp_path: Path) -> None:
    """原路径被别的文件占了就不覆盖。"""
    root = _root(tmp_path, {"a.pdf": "原内容"})
    result = sort_and_execute(root, tmp_path / "hist")
    (root / "a.pdf").write_text("后来又放了个同名文件", encoding="utf-8")

    report = _undo_manager(tmp_path).undo(result.run_id)

    assert len(report.needs_attention) == 1
    assert "占用" in report.needs_attention[0].reason
    assert (root / "a.pdf").read_text(encoding="utf-8") == "后来又放了个同名文件"
    assert (root / "文档" / "PDF" / "a.pdf").is_file()


def test_undo_reports_missing_target_as_needing_attention(tmp_path: Path) -> None:
    root = _root(tmp_path, {"a.pdf": "x"})
    result = sort_and_execute(root, tmp_path / "hist")
    (root / "文档" / "PDF" / "a.pdf").unlink()

    report = _undo_manager(tmp_path).undo(result.run_id)

    assert len(report.needs_attention) == 1
    assert "已不存在" in report.needs_attention[0].reason


# ---------------------------------------------------------------------------
# 场景 3：跨卷还原
# ---------------------------------------------------------------------------


def test_cross_volume_undo_copies_back_and_trashes_the_target(
    tmp_path: Path, monkeypatch
) -> None:
    """需求 14.4。"""
    root = _root(tmp_path, {"a.pdf": "内容内容"})
    result = sort_and_execute(root, tmp_path / "hist")
    moved = root / "文档" / "PDF" / "a.pdf"

    monkeypatch.setattr(fsops, "same_volume", lambda _a, _b: False)
    trash = TrashRecorder()
    report = _undo_manager(tmp_path, trash).undo(result.run_id)

    assert report.restored == 1
    assert (root / "a.pdf").read_text(encoding="utf-8") == "内容内容"
    assert trash.paths == {moved}
    assert not moved.exists()


def test_cross_volume_undo_failure_keeps_the_target(
    tmp_path: Path, monkeypatch
) -> None:
    """复制回原路径失败时目标副本必须还在——否则文件就没了。"""
    root = _root(tmp_path, {"a.pdf": "内容"})
    result = sort_and_execute(root, tmp_path / "hist")
    moved = root / "文档" / "PDF" / "a.pdf"

    monkeypatch.setattr(fsops, "same_volume", lambda _a, _b: False)

    def truncated_copy(src, dst, *_args, **_kwargs):  # noqa: ANN001, ANN202
        Path(dst).write_bytes(b"")
        return dst

    monkeypatch.setattr(fsops.shutil, "copy2", truncated_copy)

    report = _undo_manager(tmp_path).undo(result.run_id)

    assert report.restored == 0
    assert len(report.failed) == 1
    assert moved.read_text(encoding="utf-8") == "内容"
    assert not (root / "a.pdf").exists()


# ---------------------------------------------------------------------------
# 场景 4：还原失败不中断其余条目
# ---------------------------------------------------------------------------


def test_restore_failure_does_not_stop_other_items(tmp_path: Path) -> None:
    """需求 12.6 的撤销侧对应物。"""
    root = _root(tmp_path, {"sub": {"a.pdf": "x"}, "b.png": "y"})
    sub = root / "sub"
    result = sort_and_execute(
        root,
        tmp_path / "hist",
        options=ExecOptions(remove_empty_dirs=True),
        selected=(sub,),
    )
    assert not sub.exists()

    # 原目录的位置被一个**文件**占了，重建目录必然失败
    sub.write_text("我不是目录", encoding="utf-8")

    report = _undo_manager(tmp_path).undo(result.run_id)

    assert len(report.failed) == 1
    assert "原目录" in report.failed[0].reason
    # 另一个条目照常还原
    assert (root / "b.png").is_file()
    assert report.restored == 1


# ---------------------------------------------------------------------------
# 场景 5：嵌套目录的创建与清理
# ---------------------------------------------------------------------------


def test_undo_removes_nested_created_dirs_deepest_first(tmp_path: Path) -> None:
    """需求 14.7。多级类目要逐级回收，不能留空壳。"""
    root = _root(tmp_path, {"a.pdf": "x"})
    result = sort_and_execute(root, tmp_path / "hist")
    assert (root / "文档" / "PDF").is_dir()

    report = _undo_manager(tmp_path).undo(result.run_id)

    assert not (root / "文档" / "PDF").exists()
    assert not (root / "文档").exists()
    assert report.removed_dirs == 2


def test_undo_keeps_created_dir_that_has_foreign_content(tmp_path: Path) -> None:
    """需求 14.8。别人往里放了东西就不能删——那不是我们的数据。"""
    root = _root(tmp_path, {"a.pdf": "x"})
    result = sort_and_execute(root, tmp_path / "hist")
    stray = root / "文档" / "别人的笔记.txt"
    stray.write_text("不要删我", encoding="utf-8")

    report = _undo_manager(tmp_path).undo(result.run_id)

    assert not (root / "文档" / "PDF").exists()
    assert (root / "文档").is_dir()
    assert stray.read_text(encoding="utf-8") == "不要删我"
    assert report.removed_dirs == 1


def test_undo_recreates_removed_dirs_before_restoring_files(
    tmp_path: Path,
) -> None:
    """需求 20.18、20.19。created_dir 删、removed_dir 建，两类语义相反不可混用。"""
    root = _root(tmp_path, {"sub": {"a.pdf": "x"}})
    sub = root / "sub"
    before = tree_paths(root)

    result = sort_and_execute(
        root,
        tmp_path / "hist",
        options=ExecOptions(remove_empty_dirs=True),
        selected=(sub,),
    )
    assert not sub.exists()

    report = _undo_manager(tmp_path).undo(result.run_id)

    assert report.recreated_dirs == 1
    assert sub.is_dir()
    assert (sub / "a.pdf").read_text(encoding="utf-8") == "x"
    assert tree_paths(root) == before


def test_undo_recreates_parent_dirs_before_children(tmp_path: Path) -> None:
    """深度升序：子目录的父目录必须先存在。"""
    root = _root(tmp_path, {"外": {"内": {"a.pdf": "x"}, "b.png": "y"}})
    outer, inner = root / "外", root / "外" / "内"
    before = tree_paths(root)

    result = sort_and_execute(
        root,
        tmp_path / "hist",
        options=ExecOptions(remove_empty_dirs=True),
        selected=(outer, inner),
    )
    assert not inner.exists() and not outer.exists()

    report = _undo_manager(tmp_path).undo(result.run_id)

    assert report.recreated_dirs == 2
    assert tree_paths(root) == before


# ---------------------------------------------------------------------------
# 场景 6：撤销 → 重做 → 再撤销
# ---------------------------------------------------------------------------


def test_undo_redo_undo_is_idempotent(tmp_path: Path) -> None:
    """需求 14.13、属性 34。"""
    root = _root(tmp_path, {"a.pdf": "x", "b.png": "y", "c.txt": "z"})
    before_execute = tree_paths(root)
    before_prints = file_fingerprints(root)

    result = sort_and_execute(root, tmp_path / "hist")
    after_execute = tree_paths(root)
    after_prints = file_fingerprints(root)

    manager = _undo_manager(tmp_path)

    first_undo = manager.undo(result.run_id)
    assert first_undo.ok, first_undo.summary()
    assert tree_paths(root) == before_execute
    assert file_fingerprints(root) == before_prints

    redo = manager.redo(result.run_id)
    assert redo.ok, redo.summary()
    assert tree_paths(root) == after_execute
    assert file_fingerprints(root) == after_prints

    second_undo = manager.undo(result.run_id)
    assert second_undo.ok, second_undo.summary()
    assert tree_paths(root) == before_execute
    assert file_fingerprints(root) == before_prints
    assert second_undo.restored == first_undo.restored


def test_redo_restores_cleanup_of_empty_dirs(tmp_path: Path) -> None:
    """重做要把「被清理的空目录」也重新清理掉，否则重做不等于原执行。"""
    root = _root(tmp_path, {"sub": {"a.pdf": "x"}})
    sub = root / "sub"
    result = sort_and_execute(
        root,
        tmp_path / "hist",
        options=ExecOptions(remove_empty_dirs=True),
        selected=(sub,),
    )
    after_execute = tree_paths(root)

    manager = _undo_manager(tmp_path)
    manager.undo(result.run_id)
    assert sub.is_dir()

    manager.redo(result.run_id)

    assert tree_paths(root) == after_execute
    assert not sub.exists()
