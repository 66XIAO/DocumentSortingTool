# Feature: document-sorting-tool, Property 31: 撤销往返性
"""属性 31、32、33、34、36。

- 属性 31：撤销使每个受影响文件回到执行前的绝对路径，整树路径集合完全一致；还原
  序列是 `done` 记录的严格逆序；撤销自身产出独立 journal 且记录数与还原数一致。
- 属性 32：被外部修改的目标文件全部被跳过、位置不变、列入需人工确认并附对比信息；
  其余文件全部被正确还原。
- 属性 33：任意位置被截断的 journal，未收尾判定与「存在缺少 done/failed 的 intent」
  严格一致；全部撤销时全部 `done` 被还原，停在 `intent` 的条目被列入需人工确认。
- 属性 34：撤销 → 重做 → 再撤销产出与首次撤销相同的文件树状态。
- 属性 36：保留策略后剩余集合等于「最近 N 条且未超期」的直接筛选；逐条撤销互不影响。

## 属性 31 排除覆盖策略，这不是偷懒

覆盖策略下被覆盖的文件进了**回收站**（需求 12.4）。撤销把移动过去的文件搬回原处，
但不会、也不该从回收站里往外捞东西——那需要 Shell API 且成功率不可保证。于是「整树
路径集合与执行前完全一致」在覆盖策略下天然不成立：那个被覆盖的路径确实少了一个文件。
需求 12.4 给出的保证是「被覆盖的文件可在回收站找回」，而不是「撤销能自动找回」。因此
往返性属性的前提是非覆盖策略，这条前提写在这里而不是藏在断言里。

Validates: Requirements 13.7, 14.2, 14.3, 14.4, 14.5, 14.6, 14.9, 14.11, 14.12,
14.13, 15.2, 15.3, 15.4
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.history import HistoryManager
from app.core.journal import JOURNAL_NAME, Journal, has_unfinished_intents
from app.core.models import ConflictPolicy, ExecOptions, RecordKind, RunStatus
from app.core.undo import UndoManager
from tests.fixtures.doubles import TrashRecorder
from tests.fixtures.execution import (
    build_plan,
    execute_plan,
    file_fingerprints,
    make_history_run,
    tree_paths,
)
from tests.fixtures.generators import strategies_, tree_specs
from tests.fixtures.trees import build_tree

FS = settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.function_scoped_fixture,
    ],
)

#: 往返性只在非覆盖策略下成立，理由见模块开头
SAFE_POLICIES = st.sampled_from(
    [ConflictPolicy.AUTO_RENAME, ConflictPolicy.SKIP]
)

NOW = datetime(2026, 7, 27, 12, 0, 0).astimezone()


def _selectable(root: Path) -> list[Path]:
    return sorted(
        child
        for child in root.iterdir()
        if child.is_dir() and not child.name.startswith(".")
    )


def _draw_selection(data: st.DataObject, root: Path) -> tuple[Path, ...]:
    candidates = _selectable(root)
    if not candidates:
        return ()
    return tuple(
        data.draw(st.lists(st.sampled_from(candidates), unique=True, max_size=3))
    )


def _run(
    tmp_path_factory,
    spec: dict,
    *,
    policy: ConflictPolicy = ConflictPolicy.AUTO_RENAME,
    strategy=None,
    cleanup: bool = False,
    selected: tuple[Path, ...] | None = None,
    data: st.DataObject | None = None,
):
    """建树 → 规划 → 执行，返回 (RunResult, history, before_paths, before_prints)。"""
    from app.core.models import Strategy

    root = build_tree(tmp_path_factory.mktemp("undo"), spec).resolve()
    history = tmp_path_factory.mktemp("hist")
    if selected is None:
        selected = _draw_selection(data, root) if data is not None else ()

    before_paths = tree_paths(root)
    before_prints = file_fingerprints(root)

    plan, selection = build_plan(
        root,
        strategy=strategy or Strategy.BY_TYPE,
        conflict_policy=policy,
        selected=selected,
    )
    result = execute_plan(
        plan,
        history,
        options=ExecOptions(conflict_policy=policy, remove_empty_dirs=cleanup),
        selection=selection,
    )
    return result, history, before_paths, before_prints


# ---------------------------------------------------------------------------
# 属性 31
# ---------------------------------------------------------------------------


@FS
@given(spec=tree_specs(), policy=SAFE_POLICIES, cleanup=st.booleans(), data=st.data())
def test_undo_restores_the_whole_tree(
    tmp_path_factory, spec, policy, cleanup, data
) -> None:
    """需求 14.2、14.3。"""
    result, history, before_paths, before_prints = _run(
        tmp_path_factory, spec, policy=policy, cleanup=cleanup, data=data
    )
    if not result.report.succeeded:
        return

    report = UndoManager(history, trash=TrashRecorder()).undo(result.run_id)

    assert report.needs_attention == [], [i.reason for i in report.needs_attention]
    assert report.failed == [], [i.reason for i in report.failed]
    assert tree_paths(result.root) == before_paths
    assert file_fingerprints(result.root) == before_prints


@FS
@given(spec=tree_specs(), policy=SAFE_POLICIES, data=st.data())
def test_undo_order_is_strict_reverse_of_done(
    tmp_path_factory, spec, policy, data
) -> None:
    """属性 31：还原调用序列是 done 记录的严格逆序。"""
    result, history, _, _ = _run(
        tmp_path_factory, spec, policy=policy, data=data
    )
    forward = [r.dst for r in result.of_kind(RecordKind.DONE)]
    if not forward:
        return

    UndoManager(history, trash=TrashRecorder()).undo(result.run_id)

    undo_records = Journal.read_records(history / f"{result.run_id}.undo")
    undo_done = [r.src for r in undo_records if r.kind is RecordKind.DONE]
    assert undo_done == list(reversed(forward))


@FS
@given(spec=tree_specs(), policy=SAFE_POLICIES, data=st.data())
def test_undo_journal_record_count_matches_restored(
    tmp_path_factory, spec, policy, data
) -> None:
    """需求 14.9、14.12。"""
    result, history, _, _ = _run(
        tmp_path_factory, spec, policy=policy, data=data
    )
    report = UndoManager(history, trash=TrashRecorder()).undo(result.run_id)

    undo_records = Journal.read_records(history / f"{result.run_id}.undo")
    done = [r for r in undo_records if r.kind is RecordKind.DONE]
    assert len(done) == report.restored


@FS
@given(spec=tree_specs(), strategy=strategies_, data=st.data())
def test_undo_removes_created_dirs_and_recreates_removed_dirs(
    tmp_path_factory, spec, strategy, data
) -> None:
    """属性 35 的 M4 部分：两类目录记录语义相反，不得混用。需求 14.7、20.18。"""
    result, history, before_paths, _ = _run(
        tmp_path_factory, spec, strategy=strategy, cleanup=True, data=data
    )
    created = {Path(r.dst) for r in result.of_kind(RecordKind.CREATED_DIR) if r.dst}
    removed = {Path(r.dst) for r in result.of_kind(RecordKind.REMOVED_DIR) if r.dst}

    UndoManager(history, trash=TrashRecorder()).undo(result.run_id)

    for directory in removed:
        assert directory.is_dir(), f"removed_dir 应当被重建: {directory}"
    for directory in created:
        # created_dir 只在为空时删；不为空说明有外部残留，保留是对的
        if directory.exists():
            assert any(directory.iterdir()), f"空的 created_dir 应当被删掉: {directory}"
    assert tree_paths(result.root) == before_paths


# ---------------------------------------------------------------------------
# 属性 32
# ---------------------------------------------------------------------------


@FS
@given(spec=tree_specs(), policy=SAFE_POLICIES, data=st.data())
def test_undo_never_overwrites_externally_modified_files(
    tmp_path_factory, spec, policy, data
) -> None:
    """需求 14.5、14.6。宁可留一堆待处理项，也不冲掉用户后来的修改。"""
    result, history, _, _ = _run(
        tmp_path_factory, spec, policy=policy, data=data
    )
    targets = [Path(row.dst) for row in result.report.succeeded]
    if not targets:
        return

    tampered = set(
        data.draw(
            st.lists(
                st.sampled_from(sorted(targets)), unique=True, max_size=len(targets)
            )
        )
    )
    contents = {}
    for path in tampered:
        with open(path, "ab") as handle:
            handle.write("外部追加的内容".encode("utf-8"))
        contents[path] = path.read_bytes()

    report = UndoManager(history, trash=TrashRecorder()).undo(result.run_id)

    flagged = {Path(item.dst) for item in report.needs_attention}
    assert tampered <= flagged, f"漏报了被改过的文件: {tampered - flagged}"

    for path in tampered:
        assert path.read_bytes() == contents[path], "被改过的文件不得被移动或覆盖"

    for item in report.needs_attention:
        if Path(item.dst) in tampered:
            # 并排对比信息必须齐备，用户才能自己判断该保哪一份
            assert item.src and item.dst
            assert item.recorded_size is not None
            assert item.current_size is not None
            assert item.reason.strip()

    # 没被动过的照常还原
    for path in set(targets) - tampered:
        assert not path.exists() or path in flagged
    assert report.restored == len(targets) - len(tampered)


# ---------------------------------------------------------------------------
# 属性 33
# ---------------------------------------------------------------------------


def _naive_unfinished(records) -> bool:
    """独立实现：存在 intent 但没有对应 done/failed 的 src。

    刻意只看 done 与 failed（设计原文的口径），不看 skipped——若生产实现把 skipped
    也算作收尾会被这条比对抓出来（实际不会，因为 skipped 从不写 intent）。
    """
    intents = {r.src for r in records if r.kind is RecordKind.INTENT and r.src}
    settled = {
        r.src
        for r in records
        if r.kind in (RecordKind.DONE, RecordKind.FAILED) and r.src
    }
    return bool(intents - settled)


@FS
@given(spec=tree_specs(), cut=st.integers(min_value=0, max_value=40), data=st.data())
def test_truncated_journal_unfinished_detection(
    tmp_path_factory, spec, cut, data
) -> None:
    """需求 13.7。截断位置覆盖首条前、中间任意条、末条后。"""
    result, history, _, _ = _run(tmp_path_factory, spec, data=data)
    path = result.run_dir / JOURNAL_NAME
    lines = path.read_text(encoding="utf-8").split("\n")
    keep = [line for line in lines if line.strip()][:cut]
    path.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")

    records = Journal.read_records(result.run_dir)

    assert has_unfinished_intents(records) == _naive_unfinished(records)

    manager = HistoryManager(history)
    unfinished = {s.run_id for s in manager.find_unfinished()}
    if _naive_unfinished(records):
        assert result.run_id in unfinished
        summary = next(s for s in manager.list_runs() if s.run_id == result.run_id)
        assert summary.meta.status is RunStatus.UNFINISHED
        # 未收尾的 run 仍可撤销——那正是用户最需要它的时候
        assert summary.meta.is_undoable()
    else:
        assert result.run_id not in unfinished


@FS
@given(spec=tree_specs(), cut=st.integers(min_value=0, max_value=40), data=st.data())
def test_undo_after_truncation_restores_every_logged_move(
    tmp_path_factory, spec, cut, data
) -> None:
    """需求 14.11：全部撤销还原全部 done，停在 intent 的条目被单独指出来。"""
    result, history, _, _ = _run(tmp_path_factory, spec, data=data)
    path = result.run_dir / JOURNAL_NAME
    lines = [line for line in path.read_text("utf-8").split("\n") if line.strip()]
    path.write_text(
        "\n".join(lines[:cut]) + ("\n" if lines[:cut] else ""), encoding="utf-8"
    )

    records = Journal.read_records(result.run_dir)
    done = [r for r in records if r.kind is RecordKind.DONE and r.src and r.dst]
    pending = {
        r.src
        for r in records
        if r.kind is RecordKind.INTENT and r.src
    } - {
        r.src
        for r in records
        if r.kind in (RecordKind.DONE, RecordKind.FAILED) and r.src
    }

    manager = UndoManager(history, trash=TrashRecorder())
    resume = manager.resume(result.run_id)
    report = manager.undo(result.run_id)

    # 每条有日志依据的移动都被还原
    assert report.restored == len(done)
    for record in done:
        assert Path(record.src).exists()
        assert not Path(record.dst).exists()

    # 停在 intent 的条目一条不漏地被定位
    located = {*resume.at_source, *resume.at_target, *resume.both, *resume.missing}
    assert located == pending


# ---------------------------------------------------------------------------
# 属性 34
# ---------------------------------------------------------------------------


@FS
@given(spec=tree_specs(), policy=SAFE_POLICIES, cleanup=st.booleans(), data=st.data())
def test_undo_redo_undo_is_idempotent(
    tmp_path_factory, spec, policy, cleanup, data
) -> None:
    """需求 14.13。"""
    result, history, before_paths, before_prints = _run(
        tmp_path_factory, spec, policy=policy, cleanup=cleanup, data=data
    )
    if not result.report.succeeded:
        return

    after_paths = tree_paths(result.root)
    after_prints = file_fingerprints(result.root)
    manager = UndoManager(history, trash=TrashRecorder())

    first = manager.undo(result.run_id)
    assert first.ok, first.summary()
    assert tree_paths(result.root) == before_paths

    redo = manager.redo(result.run_id)
    assert redo.ok, redo.summary()
    assert tree_paths(result.root) == after_paths
    assert file_fingerprints(result.root) == after_prints

    second = manager.undo(result.run_id)
    assert second.ok, second.summary()
    assert tree_paths(result.root) == before_paths
    assert file_fingerprints(result.root) == before_prints
    assert second.restored == first.restored


# ---------------------------------------------------------------------------
# 属性 36
# ---------------------------------------------------------------------------


#: 年龄以**秒**为单位且互不相同。manifest 的时间戳只到秒，若两条 run 落在同一秒，
#: 「谁更新」在数据层面就无从判断——那是一条关于时间戳分辨率的属性，不是关于保留
#: 策略的属性。生成互不相同的整秒使参照模型与实现有共同的排序依据。
ages_in_seconds = st.lists(
    st.integers(min_value=0, max_value=400 * 86400),
    min_size=0,
    max_size=12,
    unique=True,
)


@FS
@given(
    ages=ages_in_seconds,
    max_runs=st.integers(min_value=1, max_value=8),
    max_days=st.integers(min_value=1, max_value=200),
)
def test_retention_keeps_exactly_the_newest_and_unexpired(
    tmp_path_factory, ages, max_runs, max_days
) -> None:
    """需求 15.3、15.4。剩余集合必须等于直接筛选的结果。"""
    history = tmp_path_factory.mktemp("hist")
    stamps: dict[str, int] = {}
    for index, age in enumerate(sorted(ages)):
        run_id = f"r{index:03d}"
        stamps[run_id] = age
        make_history_run(
            history,
            run_id,
            (NOW - timedelta(seconds=age)).isoformat(timespec="seconds"),
        )

    manager = HistoryManager(history)
    removed = manager.apply_retention(
        max_runs=max_runs, max_days=max_days, now=NOW
    )

    # 参照模型：按新→旧排序取前 max_runs 条，再剔掉超期的
    ordered = sorted(stamps, key=lambda rid: stamps[rid])
    expected_keep = {
        rid for rid in ordered[:max_runs] if stamps[rid] <= max_days * 86400
    }

    survivors = {s.run_id for s in manager.list_runs()}
    assert survivors == expected_keep
    assert set(removed) == set(stamps) - expected_keep
    for run_id in removed:
        assert not (history / run_id).exists()


@FS
@given(ages=ages_in_seconds.filter(lambda values: len(values) >= 1))
def test_retention_never_touches_unfinished_runs(
    tmp_path_factory, ages
) -> None:
    """未收尾就是用户最需要它的时候。"""
    history = tmp_path_factory.mktemp("hist")
    for index, age in enumerate(sorted(ages)):
        make_history_run(
            history,
            f"r{index:03d}",
            (NOW - timedelta(seconds=age)).isoformat(timespec="seconds"),
            unfinished=True,
        )

    manager = HistoryManager(history)
    removed = manager.apply_retention(max_runs=1, max_days=1, now=NOW)

    assert removed == []
    assert len(manager.list_runs()) == len(ages)


@settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.function_scoped_fixture,
    ],
)
@given(
    specs=st.lists(tree_specs(max_depth=1), min_size=2, max_size=3),
    victim=st.integers(min_value=0, max_value=2),
)
def test_undoing_one_run_leaves_other_runs_alone(
    tmp_path_factory, specs, victim
) -> None:
    """属性 36 的后半：逐条撤销互不影响。

    用户几天后发现「上周二那次整理错了」，撤销它不该把之后几次整理也搅乱。
    """
    history = tmp_path_factory.mktemp("hist")
    runs = []
    for index, spec in enumerate(specs):
        root = build_tree(tmp_path_factory.mktemp(f"root{index}"), spec).resolve()
        before = tree_paths(root)
        plan, selection = build_plan(root)
        result = execute_plan(
            plan, history, selection=selection, run_id=f"run-{index:02d}"
        )
        runs.append((result, before, tree_paths(root)))

    index = victim % len(runs)
    target, before, _ = runs[index]

    UndoManager(history, trash=TrashRecorder()).undo(target.run_id)

    assert tree_paths(target.root) == before
    for other_index, (other, _, after) in enumerate(runs):
        if other_index == index:
            continue
        assert tree_paths(other.root) == after, "撤销一次 run 影响了别的 run"
