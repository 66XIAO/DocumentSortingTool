"""Executor 的例子级测试。任务 32、33。

这些测试全部动真实文件——执行器的价值恰恰在于它对文件系统做了什么，用内存替身
测不出「移动完文件真在那儿」。回收站统一注入 ``TrashRecorder``，跨卷分支用
``VolumeStub`` 强制，不依赖机器上真有第二个卷。

Validates: Requirements 11.7, 11.8, 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 12.7,
12.8, 20.9, 20.10, 20.17
"""

from __future__ import annotations

from pathlib import Path

from app.core import fsops
from app.core.executor import (
    PHASE_CLEANUP,
    Executor,
    collect_cleanup_candidates,
)
from app.core.journal import Journal
from app.core.models import (
    ConflictKind,
    ConflictPolicy,
    ExecOptions,
    Outcome,
    RecordKind,
    ScanSelection,
)
from app.core.progress import CancelToken, ProgressSnapshot
from tests.fixtures.doubles import TrashRecorder
from tests.fixtures.execution import (
    build_plan,
    execute_plan,
    sort_and_execute,
    tree_paths,
)
from tests.fixtures.trees import build_tree, set_hidden


def _root(tmp_path: Path, spec: dict) -> Path:
    return build_tree(tmp_path / "root", spec).resolve()


def _hist(tmp_path: Path) -> Path:
    return tmp_path / "hist"


# ---------------------------------------------------------------------------
# 同卷移动
# ---------------------------------------------------------------------------


def test_same_volume_move_lands_file_and_writes_journal(tmp_path: Path) -> None:
    """需求 12.1、12.5、13.3、13.4。"""
    root = _root(tmp_path, {"报告.pdf": "内容"})
    result = sort_and_execute(root, _hist(tmp_path))

    moved = root / "文档" / "PDF" / "报告.pdf"
    assert moved.is_file()
    assert not (root / "报告.pdf").exists()
    assert moved.read_text(encoding="utf-8") == "内容"

    kinds = [r.kind for r in result.records()]
    assert RecordKind.INTENT in kinds
    assert RecordKind.DONE in kinds
    # 多级类目一次 mkdir 建了两层，两层都要记，否则撤销会留下空壳目录
    created = [Path(r.dst) for r in result.of_kind(RecordKind.CREATED_DIR)]
    assert set(created) == {root / "文档", root / "文档" / "PDF"}


def test_done_record_carries_size_and_precedes_no_hash_on_same_volume(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path, {"a.pdf": "12345"})
    result = sort_and_execute(root, _hist(tmp_path))

    (done,) = result.of_kind(RecordKind.DONE)
    assert done.size == 5
    assert done.mtime is not None
    # 同卷是原子重命名，没有复制发生，不该谎报一个哈希
    assert done.sha256 is None
    assert done.extra.get("cross_volume") is False


def test_every_done_has_a_smaller_seq_intent(tmp_path: Path) -> None:
    """属性 27 的例子级检查。"""
    root = _root(tmp_path, {"a.pdf": "x", "b.png": "y", "c.zzz": "z"})
    result = sort_and_execute(root, _hist(tmp_path))
    records = result.records()

    intents = {r.src: r.seq for r in records if r.kind is RecordKind.INTENT}
    for record in records:
        if record.kind in (RecordKind.DONE, RecordKind.FAILED):
            assert record.src in intents
            assert intents[record.src] < record.seq


def test_report_counts_cover_every_plan_item(tmp_path: Path) -> None:
    """需求 12.6、16.5。"""
    root = _root(tmp_path, {"a.pdf": "x", "b.png": "y", "c.zzz": "z"})
    plan, selection = build_plan(root)
    result = execute_plan(plan, _hist(tmp_path), selection=selection)

    assert result.report.total() == len(plan.all_items())
    assert len(result.report.to_csv_rows()) == result.report.total()


# ---------------------------------------------------------------------------
# 模拟运行与取消
# ---------------------------------------------------------------------------


def test_dry_run_changes_nothing_but_reports_same_counts(tmp_path: Path) -> None:
    """需求 11.8、属性 24。"""
    root = _root(tmp_path, {"a.pdf": "x", "b.png": "y", "c.zzz": "z"})
    before = tree_paths(root)

    plan, selection = build_plan(root)
    dry = execute_plan(
        plan,
        _hist(tmp_path),
        options=ExecOptions(dry_run=True),
        selection=selection,
        write_journal=False,
    )
    assert tree_paths(root) == before
    assert dry.report.dry_run is True

    # 同一份方案真实执行，三组计数应当一致
    plan2, selection2 = build_plan(root)
    real = execute_plan(plan2, _hist(tmp_path), selection=selection2)
    assert dry.report.counts() == real.report.counts()


def test_dry_run_writes_no_history(tmp_path: Path) -> None:
    root = _root(tmp_path, {"a.pdf": "x"})
    plan, selection = build_plan(root)
    execute_plan(
        plan,
        _hist(tmp_path),
        options=ExecOptions(dry_run=True),
        selection=selection,
        write_journal=False,
    )
    assert not _hist(tmp_path).exists()


def test_cancel_before_first_item_leaves_tree_untouched(tmp_path: Path) -> None:
    """需求 11.5、11.7。取消窗口内取消 = 一个文件都没动。"""
    root = _root(tmp_path, {"a.pdf": "x", "b.png": "y"})
    before = tree_paths(root)

    plan, selection = build_plan(root)
    cancel = CancelToken()
    cancel.cancel()
    result = execute_plan(plan, _hist(tmp_path), selection=selection, cancel=cancel)

    assert tree_paths(root) == before
    assert len(result.report.skipped) == len(plan.all_items())
    assert result.report.succeeded == []


def test_cancel_midway_stops_after_current_item(tmp_path: Path) -> None:
    """需求 11.7：停止在当前单文件操作完成后生效，剩下的算跳过。"""
    root = _root(tmp_path, {f"{i}.pdf": "x" for i in range(5)})
    plan, selection = build_plan(root)
    cancel = CancelToken()

    seen: list[ProgressSnapshot] = []

    def on_progress(snapshot: ProgressSnapshot) -> None:
        seen.append(snapshot)
        if snapshot.processed >= 2 and not snapshot.final:
            cancel.cancel()

    result = execute_plan(
        plan,
        _hist(tmp_path),
        selection=selection,
        cancel=cancel,
        on_progress=on_progress,
        # 节流关掉，否则 5 个小文件的中间进度会被 200ms 窗口合并掉，取消永远触发不了
        progress_interval_ms=0,
    )

    assert result.report.total() == len(plan.all_items())
    assert result.report.succeeded, "取消前已完成的条目应当保留成功状态"
    assert result.report.skipped, "取消后应当出现跳过项"
    assert seen and seen[-1].final is True
    # 已完成的确实落在目标位置，未开始的确实还在原处
    for row in result.report.succeeded:
        assert Path(row.dst).is_file()
    for row in result.report.skipped:
        assert Path(row.src).is_file()


def test_progress_final_notification_is_always_sent(tmp_path: Path) -> None:
    root = _root(tmp_path, {"a.pdf": "x"})
    plan, selection = build_plan(root)
    seen: list[ProgressSnapshot] = []

    execute_plan(
        plan, _hist(tmp_path), selection=selection, on_progress=seen.append
    )

    assert seen[-1].final is True
    assert seen[-1].phase == PHASE_CLEANUP


# ---------------------------------------------------------------------------
# 冲突策略
# ---------------------------------------------------------------------------


def test_overwrite_policy_sends_victim_to_trash_and_records_it(
    tmp_path: Path,
) -> None:
    """需求 12.4。被覆盖的文件必须可找回。"""
    root = _root(
        tmp_path,
        {"a.pdf": "新内容", "文档": {"PDF": {"a.pdf": "旧内容"}}},
    )
    victim = root / "文档" / "PDF" / "a.pdf"
    trash = TrashRecorder()

    result = sort_and_execute(
        root,
        _hist(tmp_path),
        conflict_policy=ConflictPolicy.OVERWRITE,
        trash=trash,
    )

    assert victim.read_text(encoding="utf-8") == "新内容"
    assert trash.paths == {victim}
    trashed = [Path(r.src) for r in result.of_kind(RecordKind.TRASHED)]
    assert trashed == [victim]


def test_skip_policy_leaves_both_files_in_place(tmp_path: Path) -> None:
    """需求 9.5。"""
    root = _root(
        tmp_path,
        {"a.pdf": "新内容", "文档": {"PDF": {"a.pdf": "旧内容"}}},
    )
    result = sort_and_execute(
        root, _hist(tmp_path), conflict_policy=ConflictPolicy.SKIP
    )

    assert (root / "a.pdf").read_text(encoding="utf-8") == "新内容"
    assert (root / "文档" / "PDF" / "a.pdf").read_text(encoding="utf-8") == "旧内容"
    assert result.report.succeeded == []
    assert [row.outcome for row in result.report.skipped] == [Outcome.SKIPPED]


def test_auto_rename_policy_moves_to_numbered_name(tmp_path: Path) -> None:
    """需求 9.4。"""
    root = _root(
        tmp_path,
        {"a.pdf": "新内容", "文档": {"PDF": {"a.pdf": "旧内容"}}},
    )
    sort_and_execute(
        root, _hist(tmp_path), conflict_policy=ConflictPolicy.AUTO_RENAME
    )

    assert (root / "文档" / "PDF" / "a.pdf").read_text(encoding="utf-8") == "旧内容"
    assert (root / "文档" / "PDF" / "a (2).pdf").read_text(encoding="utf-8") == "新内容"


def test_unincluded_item_is_skipped_and_recorded(tmp_path: Path) -> None:
    """需求 10.10。"""
    root = _root(tmp_path, {"a.pdf": "x", "b.png": "y"})
    plan, selection = build_plan(root)
    for item in plan.all_items():
        if item.entry.name == "a.pdf":
            item.included = False

    result = execute_plan(plan, _hist(tmp_path), selection=selection)

    assert (root / "a.pdf").is_file()
    assert (root / "图片" / "b.png").is_file()
    skipped = [Path(r.src) for r in result.of_kind(RecordKind.SKIPPED)]
    assert skipped == [root / "a.pdf"]


# ---------------------------------------------------------------------------
# 跨卷
# ---------------------------------------------------------------------------


def test_cross_volume_copies_verifies_and_trashes_source(
    tmp_path: Path, monkeypatch
) -> None:
    """需求 12.2、12.8。"""
    root = _root(tmp_path, {"a.pdf": "内容内容内容"})
    plan, selection = build_plan(root)
    source = root / "a.pdf"
    expected_hash = fsops.sha256_of(source)

    monkeypatch.setattr(fsops, "same_volume", lambda _a, _b: False)
    trash = TrashRecorder()
    result = execute_plan(
        plan, _hist(tmp_path), selection=selection, trash=trash
    )

    target = root / "文档" / "PDF" / "a.pdf"
    assert target.read_text(encoding="utf-8") == "内容内容内容"
    assert trash.paths == {source}
    (done,) = result.of_kind(RecordKind.DONE)
    assert done.sha256 == expected_hash
    assert done.extra.get("cross_volume") is True


def test_cross_volume_verify_failure_keeps_source_and_removes_partial(
    tmp_path: Path, monkeypatch
) -> None:
    """需求 12.3、属性 26。校验不一致时宁可什么都没做成。"""
    root = _root(tmp_path, {"a.pdf": "完整内容"})
    plan, selection = build_plan(root)

    monkeypatch.setattr(fsops, "same_volume", lambda _a, _b: False)

    def truncated_copy(src, dst, *_args, **_kwargs):  # noqa: ANN001, ANN202
        Path(dst).write_bytes(b"")
        return dst

    monkeypatch.setattr(fsops.shutil, "copy2", truncated_copy)

    trash = TrashRecorder()
    result = execute_plan(
        plan, _hist(tmp_path), selection=selection, trash=trash
    )

    assert (root / "a.pdf").read_text(encoding="utf-8") == "完整内容"
    assert not (root / "文档" / "PDF" / "a.pdf").exists()
    assert trash.paths == set()
    assert len(result.report.failed) == 1
    assert result.of_kind(RecordKind.FAILED)


def test_cross_volume_trash_failure_keeps_both_copies(
    tmp_path: Path, monkeypatch
) -> None:
    """源没能进回收站时两份都保留——删哪份都有丢数据的风险。"""
    root = _root(tmp_path, {"a.pdf": "内容"})
    plan, selection = build_plan(root)
    monkeypatch.setattr(fsops, "same_volume", lambda _a, _b: False)

    class Failing:
        def __call__(self, path: Path) -> None:  # noqa: ARG002
            raise OSError("回收站不可用")

        trashed: list[Path] = []

        @property
        def paths(self) -> set[Path]:
            return set()

    result = execute_plan(
        plan,
        _hist(tmp_path),
        selection=selection,
        trash=Failing(),  # type: ignore[arg-type]
    )

    assert (root / "a.pdf").is_file()
    assert (root / "文档" / "PDF" / "a.pdf").is_file()
    assert len(result.report.failed) == 1


# ---------------------------------------------------------------------------
# 单条失败不影响其余
# ---------------------------------------------------------------------------


def test_missing_source_fails_that_item_only(tmp_path: Path) -> None:
    """需求 12.6。"""
    root = _root(tmp_path, {"a.pdf": "x", "b.png": "y"})
    plan, selection = build_plan(root)
    (root / "a.pdf").unlink()  # 规划之后、执行之前被别人删了

    result = execute_plan(plan, _hist(tmp_path), selection=selection)

    assert len(result.report.failed) == 1
    assert (root / "图片" / "b.png").is_file()
    # 属性 27：failed 之前仍有一条 intent
    kinds = [r.kind for r in result.records() if r.src == str(root / "a.pdf")]
    assert kinds == [RecordKind.INTENT, RecordKind.FAILED]


def test_target_directory_creation_failure_marks_item_failed(
    tmp_path: Path,
) -> None:
    """目标目录建不出来时该条目 failed，其余条目照旧。"""
    root = _root(tmp_path, {"a.pdf": "x", "b.png": "y"})
    plan, selection = build_plan(root)
    # 在类目目录的位置放一个同名文件，mkdir 必然失败
    (root / "文档").write_text("占位", encoding="utf-8")

    result = execute_plan(plan, _hist(tmp_path), selection=selection)

    assert len(result.report.failed) == 1
    assert (root / "a.pdf").is_file()
    assert (root / "图片" / "b.png").is_file()


def test_locked_conflict_is_skipped_without_touching_the_file(
    tmp_path: Path,
) -> None:
    """需求 9.9。被占用的源文件只跳过，不尝试移动。"""
    root = _root(tmp_path, {"a.pdf": "x"})
    plan, selection = build_plan(root)
    for item in plan.all_items():
        item.conflict = ConflictKind.LOCKED

    result = execute_plan(plan, _hist(tmp_path), selection=selection)

    assert (root / "a.pdf").is_file()
    assert len(result.report.skipped) == 1
    assert "占用" in result.report.skipped[0].reason


# ---------------------------------------------------------------------------
# 空目录清理
# ---------------------------------------------------------------------------


def test_cleanup_removes_selected_folder_that_became_empty(tmp_path: Path) -> None:
    """需求 20.6、20.8。两层勾选齐备才清理。"""
    root = _root(tmp_path, {"sub": {"a.pdf": "x"}})
    sub = root / "sub"

    result = sort_and_execute(
        root,
        _hist(tmp_path),
        options=ExecOptions(remove_empty_dirs=True),
        selected=(sub,),
    )

    assert (root / "文档" / "PDF" / "a.pdf").is_file()
    assert not sub.exists()
    assert result.report.removed_dirs == [sub]
    removed = [Path(r.dst) for r in result.of_kind(RecordKind.REMOVED_DIR)]
    assert removed == [sub]


def test_cleanup_off_keeps_the_emptied_folder(tmp_path: Path) -> None:
    """需求 20.17。"""
    root = _root(tmp_path, {"sub": {"a.pdf": "x"}})
    sub = root / "sub"

    result = sort_and_execute(
        root,
        _hist(tmp_path),
        options=ExecOptions(remove_empty_dirs=False),
        selected=(sub,),
    )

    assert sub.is_dir()
    assert result.report.removed_dirs == []


def test_cleanup_skips_unselected_folder(tmp_path: Path) -> None:
    """需求 20.10、20.13。没勾选就不在候选集合里。"""
    root = _root(tmp_path, {"sub": {"a.pdf": "x"}})
    result = sort_and_execute(
        root, _hist(tmp_path), options=ExecOptions(remove_empty_dirs=True)
    )

    assert (root / "sub" / "a.pdf").is_file()
    assert result.report.removed_dirs == []


def test_cleanup_keeps_folder_with_system_junk(tmp_path: Path) -> None:
    """需求 20.12。整理后仅剩 desktop.ini 的目录算非空，必须保留。

    desktop.ini 必须真的带隐藏属性——现实中它就是隐藏的，因此不进扫描范围、不会被
    搬走，清理阶段面对的正是「只剩一个系统文件」的目录。把它建成普通文件反而测不到
    这一条：普通 desktop.ini 会被归入 `_未分类` 一起搬走，目录就真的空了。

    这条同时证明我们**没有**维护「可忽略文件白名单」——单一口径（不含任何条目才算
    空）天然满足 20.12，白名单方案反而会违反它。
    """
    root = _root(tmp_path, {"sub": {"a.pdf": "x", "desktop.ini": "junk"}})
    sub = root / "sub"
    assert set_hidden(sub / "desktop.ini"), "本测试依赖 Windows 隐藏属性"

    sort_and_execute(
        root,
        _hist(tmp_path),
        options=ExecOptions(remove_empty_dirs=True),
        selected=(sub,),
    )

    assert (root / "文档" / "PDF" / "a.pdf").is_file()
    assert sub.is_dir()
    assert (sub / "desktop.ini").is_file()


def test_cleanup_keeps_root_itself(tmp_path: Path) -> None:
    """需求 20.14。"""
    root = _root(tmp_path, {"a.pdf": "x"})
    sort_and_execute(
        root, _hist(tmp_path), options=ExecOptions(remove_empty_dirs=True)
    )
    assert root.is_dir()


def test_cleanup_candidates_is_pure_set_arithmetic(tmp_path: Path) -> None:
    """候选集合是推导出来的，不含 I/O。需求 20.6。"""
    root = tmp_path / "root"
    selection = ScanSelection(root=root, selected={root / "a", root / "b"})

    candidates = collect_cleanup_candidates(
        [root / "a", root / "c", root], selection, root
    )

    assert candidates == [root / "a"]


def test_dry_run_predicts_cleanup_without_removing(tmp_path: Path) -> None:
    """需求 20.20、属性 38。"""
    root = _root(tmp_path, {"sub": {"a.pdf": "x"}})
    sub = root / "sub"
    plan, selection = build_plan(root, selected=(sub,))

    dry = execute_plan(
        plan,
        _hist(tmp_path),
        options=ExecOptions(dry_run=True, remove_empty_dirs=True),
        selection=selection,
        write_journal=False,
    )

    assert sub.is_dir(), "模拟运行不得真的删目录"
    assert dry.report.removed_dirs == []
    assert dry.report.predicted_removed_dirs == [sub]

    # 同一份树真实执行，实际删除集合应与预测一致
    plan2, selection2 = build_plan(root, selected=(sub,))
    real = execute_plan(
        plan2,
        _hist(tmp_path),
        options=ExecOptions(remove_empty_dirs=True),
        selection=selection2,
    )
    assert real.report.removed_dirs == dry.report.predicted_removed_dirs


# ---------------------------------------------------------------------------
# 无 journal 也能跑（CLI 与模拟运行）
# ---------------------------------------------------------------------------


def test_executor_works_without_journal(tmp_path: Path) -> None:
    root = _root(tmp_path, {"a.pdf": "x"})
    plan, selection = build_plan(root)

    report = Executor(trash=TrashRecorder()).run(
        plan, journal=None, options=ExecOptions(), selection=selection
    )

    assert (root / "文档" / "PDF" / "a.pdf").is_file()
    assert report.total() == 1
    assert not Journal.read_records(tmp_path / "hist")
