# Feature: document-sorting-tool, Property 37: 空目录删除集合的精确性
"""属性 37、38、39：源侧空目录清理。

- 属性 37：实际被删除的目录集合恰好等于需求 20 那套四条件定义的规范集合。
- 属性 38：确认对话框与模拟运行列出的待删清单等于真实执行实际删除的集合；
  模拟运行后整树的目录集合与运行前完全一致。
- 属性 39：空判定的唯一口径就是「该目录不含任何条目」。

## 为什么这三条必须一起测

需求 20 的红线是「目录结构默认不被改动」。它靠三件事共同成立：删的集合必须精确
（37）、给用户看的清单必须等于真正要删的（38）、判定空的口径必须只有一个（39）。
少了任何一条，用户在确认框里看到的就不是将要发生的事。

这套测试直接来自一个真实缺陷：``Planner.EmptyDirPredictor`` 曾有一套独立实现，
它不累积「已判定会被删的子目录」，因此在嵌套情形下比实际删除**少报**——确认框说删
两个，实际删五个。现在预览、模拟运行与真实执行共用 ``app.core.cleanup``。

Validates: Requirements 20.5, 20.6, 20.7, 20.8, 20.9, 20.10, 20.11, 20.12,
20.14, 20.15, 20.17, 20.20
"""

from __future__ import annotations

from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core import cleanup, fsops
from app.core.models import ExecOptions
from tests.fixtures.execution import (
    build_plan,
    dirs_under,
    execute_plan,
    tree_paths,
)
from tests.fixtures.generators import conflict_policies, tree_specs
from tests.fixtures.models import naive_expected_removed_dirs
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


def _root(factory, spec: dict) -> Path:
    return build_tree(factory.mktemp("clean"), spec).resolve()


def _top_level_dirs(root: Path) -> set[Path]:
    return {
        child
        for child in root.iterdir()
        if child.is_dir() and child.name != METADATA
    }


def _draw_selection(data: st.DataObject, root: Path) -> tuple[Path, ...]:
    candidates = sorted(_top_level_dirs(root))
    if not candidates:
        return ()
    picked = data.draw(
        st.lists(st.sampled_from(candidates), unique=True, max_size=len(candidates))
    )
    return tuple(picked)


def _moved_pairs(report) -> list[tuple[Path, Path]]:
    return [(Path(row.src), Path(row.dst)) for row in report.succeeded if row.dst]


# ---------------------------------------------------------------------------
# 属性 37：删除集合的精确性
# ---------------------------------------------------------------------------


@FS
@given(spec=tree_specs(), data=st.data())
def test_removed_dirs_match_naive_specification(
    tmp_path_factory, spec, data
) -> None:
    """实际删除集合 == 需求 20 四条件的直译参照实现。"""
    root = _root(tmp_path_factory, spec)
    selected = _draw_selection(data, root)
    before = tree_paths(root)

    plan, selection = build_plan(root, selected=selected)
    result = execute_plan(
        plan,
        tmp_path_factory.mktemp("hist"),
        options=ExecOptions(remove_empty_dirs=True),
        selection=selection,
    )

    expected = naive_expected_removed_dirs(
        before, _moved_pairs(result.report), selection.selected_closure(), root
    )
    assert set(result.report.removed_dirs) == expected


@FS
@given(spec=tree_specs(), data=st.data())
def test_cleanup_disabled_removes_nothing(tmp_path_factory, spec, data) -> None:
    """需求 20.17：开关为 false 时保留根目录内的全部目录。"""
    root = _root(tmp_path_factory, spec)
    selected = _draw_selection(data, root)
    before = dirs_under(root)

    plan, selection = build_plan(root, selected=selected)
    result = execute_plan(
        plan,
        tmp_path_factory.mktemp("hist"),
        options=ExecOptions(remove_empty_dirs=False),
        selection=selection,
    )

    assert result.report.removed_dirs == []
    assert before <= dirs_under(root)


@FS
@given(spec=tree_specs())
def test_empty_selection_removes_nothing(tmp_path_factory, spec) -> None:
    """需求 20.5、20.10：没勾选任何子文件夹时不可能删除任何目录。"""
    root = _root(tmp_path_factory, spec)
    before = dirs_under(root)

    plan, selection = build_plan(root, selected=())
    result = execute_plan(
        plan,
        tmp_path_factory.mktemp("hist"),
        options=ExecOptions(remove_empty_dirs=True),
        selection=selection,
    )

    assert result.report.removed_dirs == []
    assert before <= dirs_under(root)


@FS
@given(spec=tree_specs(), data=st.data())
def test_root_is_never_removed(tmp_path_factory, spec, data) -> None:
    """需求 20.14：全部配置组合下都保留根目录本身。"""
    root = _root(tmp_path_factory, spec)
    selected = _draw_selection(data, root)

    plan, selection = build_plan(root, selected=selected)
    result = execute_plan(
        plan,
        tmp_path_factory.mktemp("hist"),
        options=ExecOptions(remove_empty_dirs=True),
        selection=selection,
    )

    assert root not in set(result.report.removed_dirs)
    assert root.is_dir()


@FS
@given(spec=tree_specs(), data=st.data())
def test_unselected_dirs_are_never_removed(tmp_path_factory, spec, data) -> None:
    """需求 20.13：未勾选的子文件夹及其内部目录一律保留。"""
    root = _root(tmp_path_factory, spec)
    selected = _draw_selection(data, root)

    plan, selection = build_plan(root, selected=selected)
    result = execute_plan(
        plan,
        tmp_path_factory.mktemp("hist"),
        options=ExecOptions(remove_empty_dirs=True),
        selection=selection,
    )

    closure = selection.selected_closure()
    assert all(d in closure for d in result.report.removed_dirs)


@FS
@given(spec=tree_specs(), data=st.data())
def test_already_empty_dirs_are_kept(tmp_path_factory, spec, data) -> None:
    """需求 20.9：执行前就已为空、本次也没移出文件的目录必须保留。"""
    root = _root(tmp_path_factory, spec)
    selected = _draw_selection(data, root)
    # 造一个执行前就为空的目录，并且不会有任何文件从它移出
    pristine = root / "本来就空"
    pristine.mkdir(exist_ok=True)

    plan, selection = build_plan(root, selected=selected)
    execute_plan(
        plan,
        tmp_path_factory.mktemp("hist"),
        options=ExecOptions(remove_empty_dirs=True),
        selection=selection,
    )

    assert pristine.is_dir()


# ---------------------------------------------------------------------------
# 属性 38：预测与实际一致
# ---------------------------------------------------------------------------


@FS
@given(spec=tree_specs(), data=st.data())
def test_dry_run_prediction_equals_real_removal(
    tmp_path_factory, spec, data
) -> None:
    """模拟运行列出的待删清单 == 真实执行实际删除的集合。"""
    root = _root(tmp_path_factory, spec)
    selected = _draw_selection(data, root)

    plan, selection = build_plan(root, selected=selected)
    dry = execute_plan(
        plan,
        tmp_path_factory.mktemp("hist"),
        options=ExecOptions(dry_run=True, remove_empty_dirs=True),
        selection=selection,
        write_journal=False,
    )

    # 模拟运行不该动任何东西，因此可以接着在同一棵树上真跑
    real = execute_plan(
        plan,
        tmp_path_factory.mktemp("hist2"),
        options=ExecOptions(remove_empty_dirs=True),
        selection=selection,
    )

    assert set(dry.report.predicted_removed_dirs) == set(real.report.removed_dirs)


@FS
@given(spec=tree_specs(), data=st.data())
def test_confirm_dialog_list_equals_real_removal(
    tmp_path_factory, spec, data
) -> None:
    """需求 20.7：确认对话框列出的清单 == 真实执行实际删除的集合。

    确认框的数据来源是 ``cleanup.predict_for_plan``，这条断言就是「用户看到的
    等于将要发生的」。
    """
    root = _root(tmp_path_factory, spec)
    selected = _draw_selection(data, root)

    plan, selection = build_plan(root, selected=selected)
    shown = cleanup.predict_for_plan(plan, selection)

    real = execute_plan(
        plan,
        tmp_path_factory.mktemp("hist"),
        options=ExecOptions(remove_empty_dirs=True),
        selection=selection,
    )

    assert set(shown) == set(real.report.removed_dirs)


@FS
@given(spec=tree_specs(), policy=conflict_policies, data=st.data())
def test_dry_run_keeps_every_directory(
    tmp_path_factory, spec, policy, data
) -> None:
    """需求 20.20：模拟运行只列清单，整树的目录集合保持不变。"""
    root = _root(tmp_path_factory, spec)
    selected = _draw_selection(data, root)
    before = dirs_under(root)

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

    assert dirs_under(root) == before


@FS
@given(spec=tree_specs(), data=st.data())
def test_prediction_never_understates_removal(
    tmp_path_factory, spec, data
) -> None:
    """预测**少报**是最危险的偏差方向，单独钉一条。

    曾经的缺陷正是这个形态：确认框说删两个，实际删五个。
    """
    root = _root(tmp_path_factory, spec)
    selected = _draw_selection(data, root)

    plan, selection = build_plan(root, selected=selected)
    shown = set(cleanup.predict_for_plan(plan, selection))

    real = execute_plan(
        plan,
        tmp_path_factory.mktemp("hist"),
        options=ExecOptions(remove_empty_dirs=True),
        selection=selection,
    )

    assert set(real.report.removed_dirs) <= shown


# ---------------------------------------------------------------------------
# 属性 39：空判定的唯一口径
# ---------------------------------------------------------------------------

_ENTRY_NAMES = st.sampled_from(
    ["a.txt", "desktop.ini", "Thumbs.db", "子目录", ".hidden", "b.pdf"]
)


@FS
@given(entries=st.lists(_ENTRY_NAMES, unique=True, max_size=6))
def test_emptiness_is_exactly_absence_of_entries(
    tmp_path_factory, entries: list[str]
) -> None:
    """需求 20.11、20.12：空 == 不含任何条目。系统生成文件也算条目。"""
    directory = tmp_path_factory.mktemp("empty_probe") / "d"
    directory.mkdir()
    for name in entries:
        target = directory / name
        if name == "子目录":
            target.mkdir()
        else:
            target.write_text("x", encoding="utf-8")

    assert fsops.is_effectively_empty(directory) is (not entries)


@FS
@given(entries=st.lists(_ENTRY_NAMES, unique=True, min_size=1, max_size=6))
def test_system_generated_files_make_dir_non_empty(
    tmp_path_factory, entries: list[str]
) -> None:
    """需求 20.12：仅含 desktop.ini / Thumbs.db 的目录判为非空。"""
    directory = tmp_path_factory.mktemp("sysfiles") / "d"
    directory.mkdir()
    for name in ("desktop.ini", "Thumbs.db"):
        (directory / name).write_text("x", encoding="utf-8")

    assert fsops.is_effectively_empty(directory) is False


@FS
@given(
    listing=st.lists(st.integers(0, 5), unique=True, max_size=6),
    moved=st.lists(st.integers(0, 5), unique=True, max_size=6),
)
def test_predicted_emptiness_is_set_difference(
    listing: list[int], moved: list[int]
) -> None:
    """预测形态的口径：减去将移出的条目后是否为空，就是集合差为空。"""
    dir_listing = frozenset(Path(f"/x/{i}") for i in listing)
    moved_out = frozenset(Path(f"/x/{i}") for i in moved)

    assert fsops.is_predicted_empty(dir_listing, moved_out) is (
        not (dir_listing - moved_out)
    )
