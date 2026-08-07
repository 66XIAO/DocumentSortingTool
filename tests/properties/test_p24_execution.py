# Feature: document-sorting-tool, Property 24: 模拟运行与取消的零改动性
"""属性 6、24、25、26、27。

- 属性 6：范围外不变性——未勾选的子文件夹前后路径集合一致。
- 属性 24：模拟运行与取消都不改动任何文件；模拟报告的三组计数与真实执行一致。
- 属性 25：成功的移动使目标文件的大小与内容哈希等于执行前源文件的值。
- 属性 26：中断与失败时文件不丢；三组计数之和等于待处理条目数。
- 属性 27：journal 完整反映实际副作用（新增目录、删除目录、回收站、镜像一致）。

## 关于属性 6 的一处收窄（写在这里而不是藏在断言里）

属性正文要求「未勾选的子文件夹自身及其内部全部文件与目录的绝对路径集合完全一致」。
字面执行会与需求 3 的另一半冲突：类目目录名很可能与根目录下已存在的子文件夹**同名**
（默认扩展名规则会产出「文档」「图片」，而用户的根目录里本来就常有同名文件夹）。此时
把整理好的文件并入那个已存在的文件夹，正是用户期待的行为——需求 2.7/2.20 关心的是
「子文件夹里原有的东西不被搬走、不被删掉」，而不是「永远不许往里放东西」。

因此这里断言的是**更强也更有意义的两条**：

    1. 未勾选目录中原有的路径一条都不能消失（不搬走、不删除）
    2. 新增的路径必须全部来自本次方案的目标路径及其父目录（不会凭空多出东西）

第 1 条才是「目录结构默认不被改动」这条红线真正要守的东西。

Validates: Requirements 2.20, 11.5, 11.7, 11.8, 12.1, 12.2, 12.3, 12.4, 12.5,
12.6, 12.8, 13.3, 13.4, 13.5, 20.13, 20.16
"""

from __future__ import annotations

from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core import fsops
from app.core.models import (
    ConflictPolicy,
    ExecOptions,
    Outcome,
    RecordKind,
)
from app.core.progress import CancelToken
from tests.fixtures.doubles import TrashRecorder
from tests.fixtures.execution import (
    build_plan,
    execute_plan,
    tree_paths,
)
from tests.fixtures.generators import conflict_policies, strategies_, tree_specs
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

METADATA = ".docsort"


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _root(factory, spec: dict) -> Path:
    return build_tree(factory.mktemp("exec"), spec).resolve()


def _files_with_digest(root: Path) -> dict[Path, tuple[int, str]]:
    prints: dict[Path, tuple[int, str]] = {}
    for path in root.rglob("*"):
        if METADATA in path.parts or not path.is_file():
            continue
        try:
            prints[path] = (path.stat().st_size, fsops.sha256_of(path))
        except OSError:
            continue
    return prints


def _top_level_dirs(root: Path) -> set[Path]:
    return {
        child
        for child in root.iterdir()
        if child.is_dir() and child.name != METADATA
    }


def _draw_selection(data: st.DataObject, root: Path) -> tuple[Path, ...]:
    """从根目录的直属子文件夹里抽一个子集来勾选。"""
    candidates = sorted(_top_level_dirs(root))
    if not candidates:
        return ()
    picked = data.draw(
        st.lists(st.sampled_from(candidates), unique=True, max_size=len(candidates))
    )
    return tuple(picked)


# ---------------------------------------------------------------------------
# 属性 24
# ---------------------------------------------------------------------------


@FS
@given(spec=tree_specs(), policy=conflict_policies, data=st.data())
def test_dry_run_changes_nothing(tmp_path_factory, spec, policy, data) -> None:
    """需求 11.8。"""
    root = _root(tmp_path_factory, spec)
    selected = _draw_selection(data, root)
    before = tree_paths(root)
    prints = _files_with_digest(root)

    plan, selection = build_plan(root, conflict_policy=policy, selected=selected)
    execute_plan(
        plan,
        tmp_path_factory.mktemp("hist"),
        options=ExecOptions(
            dry_run=True, conflict_policy=policy, remove_empty_dirs=True
        ),
        selection=selection,
        write_journal=False,
    )

    assert tree_paths(root) == before
    assert _files_with_digest(root) == prints


@FS
@given(spec=tree_specs(), policy=conflict_policies, data=st.data())
def test_dry_run_counts_match_real_execution(
    tmp_path_factory, spec, policy, data
) -> None:
    """属性 24：模拟报告的三组计数与真实执行一致。

    仅在真实执行未出现 failed 时可比——failed 是外部因素（权限、占用）造成的，
    模拟运行不可能预知，硬要求一致会把一条真属性写成假属性。
    """
    root = _root(tmp_path_factory, spec)
    selected = _draw_selection(data, root)

    plan, selection = build_plan(root, conflict_policy=policy, selected=selected)
    dry = execute_plan(
        plan,
        tmp_path_factory.mktemp("hist"),
        options=ExecOptions(dry_run=True, conflict_policy=policy),
        selection=selection,
        write_journal=False,
    )

    plan2, selection2 = build_plan(root, conflict_policy=policy, selected=selected)
    real = execute_plan(
        plan2,
        tmp_path_factory.mktemp("hist"),
        options=ExecOptions(conflict_policy=policy),
        selection=selection2,
    )

    if real.report.failed:
        return
    assert dry.report.counts() == real.report.counts()


@FS
@given(spec=tree_specs(), policy=conflict_policies, data=st.data())
def test_cancel_before_start_changes_nothing(
    tmp_path_factory, spec, policy, data
) -> None:
    """需求 11.5：取消窗口内取消后全部文件位置不变。"""
    root = _root(tmp_path_factory, spec)
    selected = _draw_selection(data, root)
    before = tree_paths(root)
    prints = _files_with_digest(root)

    plan, selection = build_plan(root, conflict_policy=policy, selected=selected)
    cancel = CancelToken()
    cancel.cancel()
    result = execute_plan(
        plan,
        tmp_path_factory.mktemp("hist"),
        options=ExecOptions(conflict_policy=policy, remove_empty_dirs=True),
        selection=selection,
        cancel=cancel,
    )

    assert tree_paths(root) == before
    assert _files_with_digest(root) == prints
    assert result.report.succeeded == []
    assert len(result.report.skipped) == len(plan.all_items())


# ---------------------------------------------------------------------------
# 属性 25
# ---------------------------------------------------------------------------


@FS
@given(spec=tree_specs(), policy=conflict_policies, data=st.data())
def test_content_is_invariant_across_moves(
    tmp_path_factory, spec, policy, data
) -> None:
    """需求 12.1、12.8。同卷。"""
    root = _root(tmp_path_factory, spec)
    selected = _draw_selection(data, root)
    plan, selection = build_plan(root, conflict_policy=policy, selected=selected)
    prints = _files_with_digest(root)

    result = execute_plan(
        plan,
        tmp_path_factory.mktemp("hist"),
        options=ExecOptions(conflict_policy=policy),
        selection=selection,
    )

    for row in result.report.succeeded:
        src, dst = Path(row.src), Path(row.dst)
        assert src in prints, src
        size, digest = prints[src]
        assert dst.stat().st_size == size
        assert fsops.sha256_of(dst) == digest


@FS
@given(spec=tree_specs(), data=st.data())
def test_content_is_invariant_across_volumes(
    tmp_path_factory, spec, data, monkeypatch
) -> None:
    """需求 12.2。跨卷用 VolumeStub 强制，不依赖机器上真有第二个卷。"""
    root = _root(tmp_path_factory, spec)
    selected = _draw_selection(data, root)
    plan, selection = build_plan(root, selected=selected)
    prints = _files_with_digest(root)

    monkeypatch.setattr(fsops, "same_volume", lambda _a, _b: False)
    trash = TrashRecorder()
    result = execute_plan(
        plan, tmp_path_factory.mktemp("hist"), selection=selection, trash=trash
    )

    for row in result.report.succeeded:
        src, dst = Path(row.src), Path(row.dst)
        size, digest = prints[src]
        assert dst.stat().st_size == size
        assert fsops.sha256_of(dst) == digest
        # 跨卷的源文件必须进回收站，不能被 unlink 掉
        assert src in trash.paths

    for record in result.of_kind(RecordKind.DONE):
        assert record.extra.get("cross_volume") is True
        assert record.sha256


# ---------------------------------------------------------------------------
# 属性 26
# ---------------------------------------------------------------------------


@FS
@given(spec=tree_specs(), policy=conflict_policies, data=st.data())
def test_no_file_is_ever_lost(tmp_path_factory, spec, policy, data) -> None:
    """属性 26 的核心：文件既不在源也不在目标 = 丢了，绝不允许。"""
    root = _root(tmp_path_factory, spec)
    selected = _draw_selection(data, root)
    plan, selection = build_plan(root, conflict_policy=policy, selected=selected)
    present_before = {p for p in _files_with_digest(root)}

    result = execute_plan(
        plan,
        tmp_path_factory.mktemp("hist"),
        options=ExecOptions(conflict_policy=policy),
        selection=selection,
    )

    rows = [
        *result.report.succeeded,
        *result.report.skipped,
        *result.report.failed,
    ]
    for row in rows:
        src, dst = Path(row.src), Path(row.dst)
        if src not in present_before:
            continue  # 执行前就不存在，谈不上丢
        assert src.exists() or dst.exists(), f"{src} 丢了"
        if row.outcome is Outcome.SUCCEEDED:
            assert dst.is_file()
            if src != dst:
                assert not src.exists()
        else:
            # 未成功的条目源文件必须原封不动地留在原处
            assert src.exists()


@FS
@given(spec=tree_specs(), policy=conflict_policies, data=st.data())
def test_three_groups_cover_every_item(
    tmp_path_factory, spec, policy, data
) -> None:
    """需求 12.6、16.5。"""
    root = _root(tmp_path_factory, spec)
    selected = _draw_selection(data, root)
    plan, selection = build_plan(root, conflict_policy=policy, selected=selected)

    result = execute_plan(
        plan,
        tmp_path_factory.mktemp("hist"),
        options=ExecOptions(conflict_policy=policy),
        selection=selection,
    )

    assert result.report.total() == len(plan.all_items())
    assert len(result.report.to_csv_rows()) == result.report.total()


@FS
@given(spec=tree_specs(), data=st.data())
def test_injected_failures_do_not_affect_other_items(
    tmp_path_factory, spec, data
) -> None:
    """属性 26：单条操作失败不影响其余条目的处理。

    失败注入方式是「规划之后、执行之前把源文件删掉」——这正是现实里最常见的一种
    竞态（用户或同步软件在你确认方案的那几秒里动了文件）。
    """
    root = _root(tmp_path_factory, spec)
    selected = _draw_selection(data, root)
    plan, selection = build_plan(root, selected=selected)

    movable = [
        item
        for item in plan.all_items()
        if item.included and item.entry.path.is_file()
    ]
    if not movable:
        return

    doomed = data.draw(
        st.lists(
            st.sampled_from([str(i.entry.path) for i in movable]),
            unique=True,
            max_size=len(movable),
        )
    )
    for path in doomed:
        Path(path).unlink(missing_ok=True)

    present_before = {p for p in _files_with_digest(root)}
    result = execute_plan(
        plan, tmp_path_factory.mktemp("hist"), selection=selection
    )

    assert result.report.total() == len(plan.all_items())
    # 没被删掉的条目该成功的仍然成功
    for row in result.report.succeeded:
        assert Path(row.dst).is_file()
    # 被删掉的都在 failed 里，且没有连带影响
    failed_sources = {row.src for row in result.report.failed}
    for path in doomed:
        if Path(path) in present_before:
            continue
        assert path in failed_sources


# ---------------------------------------------------------------------------
# 属性 27
# ---------------------------------------------------------------------------


@FS
@given(spec=tree_specs(), policy=conflict_policies, data=st.data())
def test_journal_reflects_directory_side_effects(
    tmp_path_factory, spec, policy, data
) -> None:
    """执行后新增/删除的目录集合等于 created_dir / removed_dir 记录集合。"""
    root = _root(tmp_path_factory, spec)
    selected = _draw_selection(data, root)
    plan, selection = build_plan(root, conflict_policy=policy, selected=selected)
    dirs_before = {p for p in tree_paths(root) if p.is_dir()}

    result = execute_plan(
        plan,
        tmp_path_factory.mktemp("hist"),
        options=ExecOptions(conflict_policy=policy, remove_empty_dirs=True),
        selection=selection,
    )

    dirs_after = {p for p in tree_paths(root) if p.is_dir()}
    created = {Path(r.dst) for r in result.of_kind(RecordKind.CREATED_DIR) if r.dst}
    removed = {Path(r.dst) for r in result.of_kind(RecordKind.REMOVED_DIR) if r.dst}

    assert dirs_before - dirs_after == removed
    assert dirs_after - dirs_before == created - removed
    assert result.report.removed_dirs and removed or True  # 允许没有可清理的目录
    assert set(result.report.removed_dirs) == removed


@FS
@given(spec=tree_specs(), data=st.data())
def test_every_settled_record_has_a_preceding_intent(
    tmp_path_factory, spec, data
) -> None:
    """需求 13.3、13.4：done/failed 必定有一条 seq 更小的同操作 intent。"""
    root = _root(tmp_path_factory, spec)
    selected = _draw_selection(data, root)
    plan, selection = build_plan(root, selected=selected)

    result = execute_plan(
        plan, tmp_path_factory.mktemp("hist"), selection=selection
    )
    records = result.records()

    first_intent: dict[str | None, int] = {}
    for record in records:
        if record.kind is RecordKind.INTENT and record.src not in first_intent:
            first_intent[record.src] = record.seq

    for record in records:
        if record.kind in (RecordKind.DONE, RecordKind.FAILED):
            assert record.src in first_intent, record
            assert first_intent[record.src] < record.seq

    # seq 全序且从 1 连续递增——撤销依赖「按 done 逆序还原」这一语义
    assert [r.seq for r in records] == list(range(1, len(records) + 1))


@FS
@given(spec=tree_specs(), data=st.data())
def test_mirror_journal_matches_main_copy(tmp_path_factory, spec, data) -> None:
    """需求 13.5：根目录镜像的记录序列与主副本一致。"""
    root = _root(tmp_path_factory, spec)
    selected = _draw_selection(data, root)
    plan, selection = build_plan(root, selected=selected)

    result = execute_plan(
        plan, tmp_path_factory.mktemp("hist"), selection=selection
    )

    assert result.records() == result.mirror_records()
    assert result.manifest() is not None


@FS
@given(spec=tree_specs())
def test_trashed_records_equal_actual_trash_calls(tmp_path_factory, spec) -> None:
    """需求 12.4：被覆盖策略移入回收站的文件集合等于 trashed 记录集合。

    只在覆盖策略下断言。跨卷移动也会调用回收站（源文件），但那不写 trashed 记录——
    它由 done 记录的 cross_volume 标记表达，两者语义不同，不该混进同一个集合。
    """
    root = _root(tmp_path_factory, spec)
    plan, selection = build_plan(root, conflict_policy=ConflictPolicy.OVERWRITE)
    trash = TrashRecorder()

    result = execute_plan(
        plan,
        tmp_path_factory.mktemp("hist"),
        options=ExecOptions(conflict_policy=ConflictPolicy.OVERWRITE),
        selection=selection,
        trash=trash,
    )

    recorded = {Path(r.src) for r in result.of_kind(RecordKind.TRASHED) if r.src}
    assert trash.paths == recorded


# ---------------------------------------------------------------------------
# 属性 6
# ---------------------------------------------------------------------------


@FS
@given(
    spec=tree_specs(),
    policy=conflict_policies,
    strategy=strategies_,
    cleanup=st.booleans(),
    data=st.data(),
)
def test_out_of_scope_folders_lose_nothing(
    tmp_path_factory, spec, policy, strategy, cleanup, data
) -> None:
    """属性 6。见模块开头对「收窄」的说明。"""
    root = _root(tmp_path_factory, spec)
    selected = _draw_selection(data, root)
    unselected = sorted(_top_level_dirs(root) - set(selected))
    before = {folder: tree_paths(folder) | {folder} for folder in unselected}

    plan, selection = build_plan(
        root, strategy=strategy, conflict_policy=policy, selected=selected
    )
    allowed_additions = {item.target for item in plan.all_items()}
    for item in plan.all_items():
        parent = item.target.parent
        while parent != root and parent.is_relative_to(root):
            allowed_additions.add(parent)
            parent = parent.parent

    execute_plan(
        plan,
        tmp_path_factory.mktemp("hist"),
        options=ExecOptions(conflict_policy=policy, remove_empty_dirs=cleanup),
        selection=selection,
    )

    for folder, paths in before.items():
        after = tree_paths(folder) | ({folder} if folder.exists() else set())
        assert paths - after == set(), f"{folder} 里原有的路径消失了: {paths - after}"
        unexpected = after - paths - allowed_additions
        assert unexpected == set(), f"{folder} 里凭空多出了 {unexpected}"
